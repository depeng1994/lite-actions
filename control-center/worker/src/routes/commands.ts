import {body, type Router, HttpError} from "../router";
import {moveWorkflowUp, operationView} from "../services/workflowPriority";

export function commandRoutes(router: Router) {
  router.add("GET", "/api/v1/admin/commands/:cmdId", async context => {
    const row = await context.env.DB.prepare(`
      SELECT cmd_id,worker_id,cmd_type,status,created_at,acked_at,finished_at,result_json,error_msg FROM commands WHERE cmd_id=?
    `).bind(context.params.cmdId).first<{result_json: string | null}>();
    if (!row) throw new HttpError(404, "COMMAND_NOT_FOUND");
    const {result_json, ...fields} = row;
    return Response.json({...fields, result: result_json ? JSON.parse(result_json) : null});
  });
  router.add("POST", "/api/v1/admin/workflows/move-up", async context => {
    const result = await moveWorkflowUp(context, await body(context));
    return Response.json(result, {status: result.status === "NOOP" ? 200 : 202});
  });
  router.add("GET", "/api/v1/admin/workflow-priority-ops/:opId", async context =>
    Response.json(await operationView(context, context.params.opId)));
}
