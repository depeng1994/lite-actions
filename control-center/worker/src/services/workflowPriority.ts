import {type Context, HttpError} from "../router";
import type {SqlStatement, SqlStore} from "../storage/sqlStore";
import {auditStatement} from "./audit";
import {refreshWorkflowRuns, type WorkflowIdentity as IngestWorkflowIdentity} from "./ingest";
import {integer, REPO, RUN_ID, string} from "../utils/validate";
import type {Provider, WorkerId} from "../types/api";

type WorkflowIdentity = {provider: Provider; repo: string; run_id: string; attempt: number};
type WaitingWorkflow = WorkflowIdentity & {queue_order_rank: number | null; submitted_at: number | null};
type ActiveQueuedRow = WorkflowIdentity & {
  task_uid: string; task_key: string; worker_id: WorkerId; pool_name: string;
  kind: "train" | "npu_smoke"; queue_pos: number | null;
};

function parse(input: Record<string, unknown>): WorkflowIdentity & {queue_version: number; idempotency_key: string | null} {
  const provider = input.provider;
  if (provider !== "github" && provider !== "gitcode") throw new HttpError(400, "INVALID_INPUT", "provider");
  const repo = string(input.repo, "repo", 128, REPO);
  const run_id = string(input.run_id, "run_id", 128, RUN_ID);
  const attempt = integer(input.attempt, "attempt", 1);
  const queue_version = integer(input.queue_version, "queue_version", 0);
  const idempotency_key = input.idempotency_key == null ? null :
    string(input.idempotency_key, "idempotency_key", 128, /^[A-Za-z0-9._:-]+$/);
  return {provider, repo, run_id, attempt, queue_version, idempotency_key};
}

function workflowKey(row: WorkflowIdentity) {
  return JSON.stringify([row.provider, row.repo, row.run_id, row.attempt]);
}

async function syncMissingActiveWorkflowRuns(db: SqlStore, now: number) {
  const missing = await db.prepare(`
    SELECT DISTINCT a.provider,a.repo,a.run_id,a.attempt FROM tasks_active a
    WHERE NOT EXISTS (
      SELECT 1 FROM workflow_runs w
      WHERE w.provider=a.provider AND w.repo=a.repo AND w.run_id=a.run_id AND w.attempt=a.attempt
    )
  `).all<IngestWorkflowIdentity>();
  if (missing.results.length) {
    await db.batch(refreshWorkflowRuns(db, missing.results, now));
  }
}

async function syncQueueVersion(context: Context) {
  const db = context.env.DB;
  await db.prepare(`UPDATE workflow_queue_meta SET queue_version=MAX(queue_version,
    COALESCE((SELECT MAX(queue_ver) FROM queue_orders),0)) WHERE id=1`).run();
  const row = await db.prepare("SELECT queue_version FROM workflow_queue_meta WHERE id=1")
    .first<number>("queue_version");
  return Number(row ?? 0);
}

async function waitingWorkflows(context: Context): Promise<WaitingWorkflow[]> {
  await syncMissingActiveWorkflowRuns(context.env.DB, context.now);
  const {results} = await context.env.DB.prepare(`
    SELECT provider,repo,run_id,attempt,queue_order_rank,submitted_at
    FROM workflow_runs
    WHERE is_active=1 AND has_in_flight=0 AND has_waiting_resource_case=1
    ORDER BY CASE WHEN queue_order_rank IS NULL THEN 1 ELSE 0 END,
      queue_order_rank ASC,
      COALESCE(submitted_at, 9223372036854775807) ASC,
      provider ASC, repo ASC, run_id ASC, attempt ASC
  `).all<WaitingWorkflow>();
  return results;
}

async function activeQueuedRows(db: SqlStore, workflows: WorkflowIdentity[]): Promise<ActiveQueuedRow[]> {
  if (!workflows.length) return [];
  const {results} = await db.prepare(`
    SELECT a.provider,a.repo,a.run_id,a.attempt,a.task_uid,a.task_key,a.worker_id,
      a.target_pool AS pool_name,a.kind,q.queue_pos
    FROM tasks_active a
    JOIN json_each(?) k
      ON a.provider=json_extract(k.value,'$.provider')
      AND a.repo=json_extract(k.value,'$.repo')
      AND a.run_id=json_extract(k.value,'$.run_id')
      AND a.attempt=json_extract(k.value,'$.attempt')
    LEFT JOIN queue_orders q
      ON q.task_uid=a.task_uid AND q.worker_id=a.worker_id
      AND q.pool_name=a.target_pool AND q.kind=a.kind
    WHERE a.phase='QUEUED' AND a.kind IN ('train','npu_smoke')
    ORDER BY COALESCE(q.queue_pos, 9223372036854775807) ASC, a.queued_at ASC, a.task_uid ASC
  `).bind(JSON.stringify(workflows)).all<ActiveQueuedRow>();
  return results;
}

function identity(input: WorkflowIdentity) {
  return {provider: input.provider, repo: input.repo, run_id: input.run_id, attempt: input.attempt};
}

async function existingOperation(context: Context, key: string) {
  return context.env.DB.prepare("SELECT * FROM workflow_priority_ops WHERE idempotency_key=?")
    .bind(key).first<Record<string, unknown>>();
}

async function operationView(context: Context, opId: string) {
  const db = context.env.DB;
  const op = await db.prepare("SELECT * FROM workflow_priority_ops WHERE op_id=?").bind(opId)
    .first<Record<string, unknown>>();
  if (!op) throw new HttpError(404, "WORKFLOW_PRIORITY_OP_NOT_FOUND");
  const commands = await db.prepare(`SELECT cmd_id,worker_id,status,result_json,error_msg,created_at,acked_at,finished_at
    FROM commands WHERE op_id=? ORDER BY ordinal`).bind(opId).all();
  return {
    op_id: op.op_id, status: op.status, queue_version_before: op.queue_version_before,
    queue_version_after: op.queue_version_after, from_seq: op.from_seq, to_seq: op.to_seq,
    target_workflow: JSON.parse(String(op.target_wf_json)),
    swapped_workflow: op.swapped_wf_json ? JSON.parse(String(op.swapped_wf_json)) : null,
    commands: commands.results.map(command => ({...command,
      result: command.result_json ? JSON.parse(String(command.result_json)) : null,
    })),
    ...(op.status === "NOOP" ? {reason: "ALREADY_AT_HEAD"} : {}),
  };
}

export async function moveWorkflowUp(context: Context, input: Record<string, unknown>) {
  const parsed = parse(input);
  if (parsed.idempotency_key) {
    const previous = await existingOperation(context, parsed.idempotency_key);
    if (previous) return operationView(context, String(previous.op_id));
  }
  const before = await syncQueueVersion(context);
  if (parsed.queue_version !== before) throw new HttpError(409, "QUEUE_VERSION_CONFLICT");
  const queue = await waitingWorkflows(context);
  const targetKey = workflowKey(parsed);
  const targetIndex = queue.findIndex(item => workflowKey(item) === targetKey);
  if (targetIndex < 0) throw new HttpError(409, "WORKFLOW_NOT_WAITING");
  const opId = "wpo_" + crypto.randomUUID();
  const db = context.env.DB;
  if (targetIndex === 0) {
    try {
      await db.batch([
        db.prepare(`INSERT INTO workflow_priority_ops(op_id,idempotency_key,queue_version_before,queue_version_after,
          target_wf_json,swapped_wf_json,from_seq,to_seq,status,requested_by,created_at)
          VALUES (?,?,?,?,?,?,?,?,'NOOP',?,?)`)
          .bind(opId, parsed.idempotency_key, before, before, JSON.stringify(identity(parsed)), null, 1, 1,
            context.principal!.username, context.now),
        await auditStatement(context, "MOVE_UP_WORKFLOW", "workflow", targetKey, {result: "NOOP", summary: {op_id: opId}}),
      ]);
    } catch (error) {
      if (parsed.idempotency_key && String(error).includes("UNIQUE constraint failed: workflow_priority_ops.idempotency_key")) {
        const winner = await existingOperation(context, parsed.idempotency_key);
        if (winner) return operationView(context, String(winner.op_id));
      }
      throw error;
    }
    return operationView(context, opId);
  }
  const target = queue[targetIndex];
  const predecessor = queue[targetIndex - 1];
  const predKey = workflowKey(predecessor);
  const after = before + 1;

  // Reorder waiting workflow ranks atomically so cross-pool swaps persist even
  // when the two Workflows do not share any single Worker/Pool/Kind queue.
  const reordered = [...queue];
  reordered[targetIndex - 1] = target;
  reordered[targetIndex] = predecessor;
  const rankUpdates = reordered.map((wf, index) =>
    db.prepare(`UPDATE workflow_runs SET queue_order_rank=?,updated_at=?
      WHERE provider=? AND repo=? AND run_id=? AND attempt=?`)
      .bind(index + 1, context.now, wf.provider, wf.repo, wf.run_id, wf.attempt),
  );

  // Swap complete occupied slots per shared (worker, pool, kind) queue while
  // preserving intra-Workflow case order and all other Workflows' slots.
  const pairRows = await activeQueuedRows(db, [identity(target), identity(predecessor)]);
  const targetQueues = new Set(pairRows.filter(row => workflowKey(row) === targetKey && row.queue_pos != null)
    .map(row => JSON.stringify([row.worker_id, row.pool_name, row.kind])));
  const predQueues = new Set(pairRows.filter(row => workflowKey(row) === predKey && row.queue_pos != null)
    .map(row => JSON.stringify([row.worker_id, row.pool_name, row.kind])));
  const sharedQueues = [...targetQueues].filter(key => predQueues.has(key));
  const queueUpdates: SqlStatement[] = [];
  for (const queueKey of sharedQueues) {
    const [workerId, poolName, kind] = JSON.parse(queueKey) as [WorkerId, string, "train" | "npu_smoke"];
    const {results: queueRows} = await db.prepare(`
      SELECT a.provider,a.repo,a.run_id,a.attempt,q.task_uid,q.queue_pos
      FROM queue_orders q JOIN tasks_active a ON a.task_uid=q.task_uid
      WHERE q.worker_id=? AND q.pool_name=? AND q.kind=? AND a.phase='QUEUED'
      ORDER BY q.queue_pos ASC, q.task_uid ASC
    `).bind(workerId, poolName, kind).all<WorkflowIdentity & {task_uid: string; queue_pos: number}>();
    const targetItems = queueRows.filter(row => workflowKey(row) === targetKey);
    const predItems = queueRows.filter(row => workflowKey(row) === predKey);
    if (!targetItems.length || !predItems.length) continue;
    const occupiedSlots = queueRows
      .filter(row => {
        const key = workflowKey(row);
        return key === targetKey || key === predKey;
      })
      .map(row => row.queue_pos);
    const placed = [...targetItems, ...predItems];
    for (let i = 0; i < placed.length; i += 1) {
      queueUpdates.push(db.prepare(`UPDATE queue_orders SET queue_pos=?,queue_ver=?,updated_at=?
        WHERE worker_id=? AND pool_name=? AND kind=? AND task_uid=?`)
        .bind(occupiedSlots[i], after, context.now, workerId, poolName, kind, placed[i].task_uid));
    }
  }

  const workers = new Set(pairRows.map(row => row.worker_id));
  const commandStatements = [...workers].map(worker => db.prepare(`
    INSERT INTO commands(cmd_id,worker_id,cmd_type,op_id,payload_json,status,created_by,created_at)
    VALUES (?,?,'SWAP_WORKFLOW',?,?,'PENDING',?,?)`).bind(
    "cmd_" + crypto.randomUUID(), worker, opId,
    JSON.stringify({op_id: opId, queue_version: after, a: identity(parsed), b: identity(predecessor)}),
    context.principal!.username, context.now));

  // Statement 0 enforces the optimistic lock via the NOT NULL constraint on
  // workflow_queue_meta.queue_version so any concurrent version bump rolls back
  // the entire SQLite batch without leaving dirty commands or audits.
  try {
    await db.batch([
      db.prepare(`UPDATE workflow_queue_meta
        SET queue_version=CASE WHEN queue_version=? THEN ? ELSE NULL END,updated_at=?
        WHERE id=1`).bind(before, after, context.now),
      ...rankUpdates,
      ...queueUpdates,
      db.prepare(`INSERT INTO workflow_priority_ops(op_id,idempotency_key,queue_version_before,queue_version_after,
        target_wf_json,swapped_wf_json,from_seq,to_seq,status,requested_by,created_at)
        VALUES (?,?,?,?,?,?,?,?,'PENDING',?,?)`)
        .bind(opId, parsed.idempotency_key, before, after, JSON.stringify(identity(parsed)), JSON.stringify(identity(predecessor)),
          targetIndex + 1, targetIndex, context.principal!.username, context.now),
      ...commandStatements,
      await auditStatement(context, "MOVE_UP_WORKFLOW", "workflow", targetKey, {summary: {op_id: opId, queue_version: after}}),
    ]);
  } catch (error) {
    const message = String(error);
    if (message.includes("NOT NULL constraint failed: workflow_queue_meta.queue_version")) {
      throw new HttpError(409, "QUEUE_VERSION_CONFLICT");
    }
    if (parsed.idempotency_key && message.includes("UNIQUE constraint failed: workflow_priority_ops.idempotency_key")) {
      const winner = await existingOperation(context, parsed.idempotency_key);
      if (winner) return operationView(context, String(winner.op_id));
    }
    throw error;
  }
  return operationView(context, opId);
}

export {operationView};
