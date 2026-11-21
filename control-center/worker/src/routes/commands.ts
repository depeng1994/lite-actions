import {body, type Router, HttpError} from "../router";
import {auditStatement} from "../services/audit";
import {moveWorkflowUp, operationView} from "../services/workflowPriority";
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
    const result = await moveWorkflowUp(context, await body(context));
    return Response.json(result, {status: result.status === "NOOP" ? 200 : 202});
  });
  router.add("GET", "/api/v1/admin/workflow-priority-ops/:opId", async context =>
    Response.json(await operationView(context, context.params.opId)));
}
