import {body, type Router, HttpError} from "../router";
import {auditStatement} from "../services/audit";
import {integer, string} from "../utils/validate";

export function commandRoutes(router: Router) {
  router.add("GET", "/api/v1/admin/commands/:cmdId", async context => {
    const row = await context.env.DB.prepare(`
      SELECT cmd_id,worker_id,cmd_type,status,created_at,acked_at,finished_at,result_json,error_msg FROM commands WHERE cmd_id=?
    `).bind(context.params.cmdId).first<{result_json: string | null}>();
    if (!row) throw new HttpError(404, "COMMAND_NOT_FOUND");
    const {result_json, ...fields} = row;
    return Response.json({...fields, result: result_json ? JSON.parse(result_json) : null});
  });
  router.add("POST", "/api/v1/admin/tasks/:taskUid/move-up", async context => {
    const {DB: db} = context.env;
    const uid = context.params.taskUid;
    const row = await db.prepare(`
      SELECT a.kind,a.phase,(SELECT queue_pos FROM queue_orders q WHERE q.task_uid=a.task_uid
        AND q.worker_id=a.worker_id AND q.pool_name=a.target_pool AND q.kind=a.kind) AS pos
      FROM tasks_active a WHERE a.task_uid=?
    `).bind(uid).first<{kind: string; phase: string; pos: number | null}>();
    if (!row || row.phase !== "QUEUED") {
      await (await auditStatement(context, "MOVE_UP", "task", uid, {result: "CONFLICT", error: "TASK_NOT_QUEUED"})).run();
      throw new HttpError(409, "TASK_NOT_QUEUED");
    }
    if (row.kind === "worker_probe") throw new HttpError(400, "TASK_KIND_UNSUPPORTED");
    if (row.pos === 0) {
      await (await auditStatement(context, "MOVE_UP", "task", uid, {result: "NOOP"})).run();
      return Response.json({status: "NOOP", reason: "ALREADY_AT_HEAD"});
    }
    const command = "cmd_" + crypto.randomUUID();
    const created = await db.batch([
      db.prepare(`INSERT INTO commands(cmd_id,worker_id,cmd_type,payload_json,status,created_by,created_at)
        SELECT ?,worker_id,'MOVE_UP',json_object('task_uid', task_uid,'task_key',task_key),'PENDING',?,? FROM tasks_active
        WHERE task_uid=? AND phase='QUEUED' AND kind IN ('train','npu_smoke')`)
        .bind(command, context.principal!.username, context.now, uid),
      await auditStatement(context, "MOVE_UP", "task", uid, {summary: {cmd_id: command}}),
    ]);
    if (created[0].meta.changes !== 1) throw new HttpError(409, "TASK_NOT_QUEUED");
    return Response.json({cmd_id: command, status: "PENDING"}, {status: 202});
  });
  router.add("POST", "/api/v1/admin/workflows/move-up", async context => {
    const input = await body(context);
    const provider = input.provider;
    if (provider !== "github" && provider !== "gitcode") throw new HttpError(400, "INVALID_INPUT", "provider");
    const repo = string(input.repo, "repo", 128);
    const runId = string(input.run_id, "run_id", 128);
    const attempt = integer(input.attempt, "attempt", 1);
    const version = integer(input.queue_version, "queue_version", 0);
    const {DB: db} = context.env;
    const current = await db.prepare("SELECT COALESCE(MAX(queue_ver),0) AS version FROM queue_orders").first<{version: number}>("version");
    if (version !== Number(current?.version ?? 0)) throw new HttpError(409, "QUEUE_VERSION_CONFLICT");
    const rows = await db.prepare(`SELECT a.task_uid,a.task_key,a.worker_id,a.kind,
      (SELECT queue_pos FROM queue_orders q WHERE q.task_uid=a.task_uid AND q.worker_id=a.worker_id AND q.pool_name=a.target_pool AND q.kind=a.kind) AS pos
      FROM tasks_active a WHERE a.provider=? AND a.repo=? AND a.run_id=? AND a.attempt=? AND a.phase='QUEUED'
        AND a.kind IN ('train','npu_smoke') ORDER BY pos,a.task_uid`).bind(provider, repo, runId, attempt).all<{
      task_uid: string; task_key: string; worker_id: "blue"|"yellow"; kind: string; pos: number | null;
    }>();
    const target = rows.results.find(row => row.pos != null);
    if (!target) throw new HttpError(409, "WORKFLOW_NOT_QUEUED");
    if (target.pos === 0) return Response.json({status: "NOOP", reason: "ALREADY_AT_HEAD"});
    const command = "cmd_" + crypto.randomUUID();
    const created = await db.batch([
      db.prepare(`INSERT INTO commands(cmd_id,worker_id,cmd_type,payload_json,status,created_by,created_at)
        VALUES (?,?,'MOVE_UP',?,'PENDING',?,?)`).bind(command, target.worker_id,
        JSON.stringify({task_uid: target.task_uid, task_key: target.task_key, workflow: {provider, repo, run_id: runId, attempt}, queue_version: version}),
        context.principal!.username, context.now),
      await auditStatement(context, "MOVE_UP_WORKFLOW", "workflow", `${provider}/${repo}/${runId}/${attempt}`, {summary: {cmd_id: command, task_uid: target.task_uid}}),
    ]);
    if (created[0].meta.changes !== 1) throw new HttpError(409, "WORKFLOW_NOT_QUEUED");
    return Response.json({op_id: command, cmd_id: command, status: "PENDING", queue_version: version}, {status: 202});
  });
}
