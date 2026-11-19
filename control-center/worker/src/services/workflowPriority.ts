import {type Context, HttpError} from "../router";
import {auditStatement} from "./audit";
import {integer, REPO, RUN_ID, string} from "../utils/validate";
import type {Provider, WorkerId} from "../types/api";

type WorkflowIdentity = {provider: Provider; repo: string; run_id: string; attempt: number};
type QueueRow = WorkflowIdentity & {task_uid: string; task_key: string; worker_id: WorkerId; pool_name: string;
  kind: "train" | "npu_smoke"; queue_pos: number; queue_ver: number};
type WorkflowQueue = WorkflowIdentity & {rows: QueueRow[]; position: number};

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

async function syncQueueVersion(context: Context) {
  const db = context.env.DB;
  await db.prepare(`UPDATE workflow_queue_meta SET queue_version=MAX(queue_version,
    COALESCE((SELECT MAX(queue_ver) FROM queue_orders),0)) WHERE id=1`).run();
  const row = await db.prepare("SELECT queue_version FROM workflow_queue_meta WHERE id=1")
    .first<number>("queue_version");
  return Number(row ?? 0);
}

async function queuedWorkflows(context: Context): Promise<WorkflowQueue[]> {
  const {results} = await context.env.DB.prepare(`
    SELECT a.provider,a.repo,a.run_id,a.attempt,a.task_uid,a.task_key,a.worker_id,a.target_pool AS pool_name,a.kind,
      q.queue_pos,q.queue_ver
    FROM tasks_active a JOIN queue_orders q ON q.task_uid=a.task_uid AND q.worker_id=a.worker_id
      AND q.pool_name=a.target_pool AND q.kind=a.kind
    WHERE a.phase='QUEUED' AND a.kind IN ('train','npu_smoke')
    ORDER BY q.queue_pos,a.provider,a.repo,a.run_id,a.attempt,a.task_uid
  `).all<QueueRow>();
  const grouped = new Map<string, WorkflowQueue>();
  for (const row of results) {
    const key = workflowKey(row);
    const existing = grouped.get(key);
    if (existing) {
      existing.rows.push(row);
      existing.position = Math.min(existing.position, row.queue_pos);
    } else {
      grouped.set(key, {...row, rows: [row], position: row.queue_pos});
    }
  }
  return [...grouped.values()].sort((left, right) => left.position - right.position ||
    workflowKey(left).localeCompare(workflowKey(right)));
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
  const queue = await queuedWorkflows(context);
  const targetKey = workflowKey(parsed);
  const targetIndex = queue.findIndex(item => workflowKey(item) === targetKey);
  if (targetIndex < 0) throw new HttpError(409, "WORKFLOW_NOT_WAITING");
  const opId = "wpo_" + crypto.randomUUID();
  const target = queue[targetIndex];
  if (targetIndex === 0) {
    await context.env.DB.batch([
      context.env.DB.prepare(`INSERT INTO workflow_priority_ops(op_id,idempotency_key,queue_version_before,queue_version_after,
        target_wf_json,swapped_wf_json,from_seq,to_seq,status,requested_by,created_at)
        VALUES (?,?,?,?,?,?,? ,?,'NOOP',?,?)`)
        .bind(opId, parsed.idempotency_key, before, before, JSON.stringify(identity(parsed)), null, 1, 1,
          context.principal!.username, context.now),
      await auditStatement(context, "MOVE_UP_WORKFLOW", "workflow", targetKey, {result: "NOOP", summary: {op_id: opId}}),
    ]);
    return operationView(context, opId);
  }
  const predecessor = queue[targetIndex - 1];
  const after = before + 1;
  const updates = [];
  for (const row of target.rows) {
    const peer = predecessor.rows.find(candidate => candidate.worker_id === row.worker_id &&
      candidate.pool_name === row.pool_name && candidate.kind === row.kind);
    if (!peer) continue;
    updates.push(context.env.DB.prepare(`UPDATE queue_orders SET queue_pos=?,queue_ver=?
      WHERE worker_id=? AND pool_name=? AND kind=? AND task_uid=?`).bind(peer.queue_pos, after,
        row.worker_id, row.pool_name, row.kind, row.task_uid));
    updates.push(context.env.DB.prepare(`UPDATE queue_orders SET queue_pos=?,queue_ver=?
      WHERE worker_id=? AND pool_name=? AND kind=? AND task_uid=?`).bind(row.queue_pos, after,
        peer.worker_id, peer.pool_name, peer.kind, peer.task_uid));
  }
  const workers = new Set([...target.rows, ...predecessor.rows].map(row => row.worker_id));
  const commandId = (worker: WorkerId) => "cmd_" + crypto.randomUUID();
  const commandRows = [...workers].map(worker => {
    const cmd = commandId(worker);
    return {worker, cmd, statement: context.env.DB.prepare(`INSERT INTO commands(cmd_id,worker_id,cmd_type,op_id,payload_json,status,created_by,created_at)
      SELECT ?,?,'SWAP_WORKFLOW',?,?,'PENDING',?,? WHERE EXISTS
      (SELECT 1 FROM workflow_queue_meta WHERE id=1 AND queue_version=?)`).bind(cmd, worker, opId,
        JSON.stringify({op_id: opId, queue_version: after, a: identity(parsed), b: identity(predecessor)}),
        context.principal!.username, context.now, before)};
  });
  const created = await context.env.DB.batch([
    ...updates,
    context.env.DB.prepare("UPDATE workflow_queue_meta SET queue_version=?,updated_at=? WHERE id=1 AND queue_version=?")
      .bind(after, context.now, before),
    context.env.DB.prepare(`INSERT INTO workflow_priority_ops(op_id,idempotency_key,queue_version_before,queue_version_after,
      target_wf_json,swapped_wf_json,from_seq,to_seq,status,requested_by,created_at)
      SELECT ?,?,?,?,?,?,?,?,'PENDING',?,? WHERE EXISTS
      (SELECT 1 FROM workflow_queue_meta WHERE id=1 AND queue_version=?)`)
      .bind(opId, parsed.idempotency_key, before, after, JSON.stringify(identity(parsed)), JSON.stringify(identity(predecessor)),
        targetIndex + 1, targetIndex, context.principal!.username, context.now, after),
    ...commandRows.map(row => row.statement),
    await auditStatement(context, "MOVE_UP_WORKFLOW", "workflow", targetKey, {summary: {op_id: opId, queue_version: after}}),
  ]);
  const meta = created[updates.length].meta.changes;
  const opInserted = created[updates.length + 1].meta.changes;
  if (meta !== 1 || opInserted !== 1) throw new HttpError(409, "QUEUE_VERSION_CONFLICT");
  return operationView(context, opId);
}

export {operationView};
