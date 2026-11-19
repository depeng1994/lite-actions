import {body, type Router, HttpError} from "../router";
import {ingestEvents, ingestSnapshot} from "../services/ingest";
import {array, integer, member, nullableInt, nullableString, object, POOL, string} from "../utils/validate";

export function workerRoutes(router: Router) {
  const prefix = "/api/v1/worker";
  router.add("POST", prefix + "/heartbeat", async context => {
    const input = await body(context);
    if (input.w !== context.workerId) throw new HttpError(403, "WORKER_MISMATCH");
    const instance = string(input.i, "instance", 8, /^[a-f0-9]{8}$/);
    const sha = string(input.sha ?? "", "code sha", 40, /^[a-f0-9]{0,40}$/);
    const {DB: db} = context.env;
    const now = context.now;
    const stats = await db.prepare(`
      INSERT INTO workers(worker_id,instance_id,applied_rev,code_sha,health_ok,active_count,queued_count,
        last_heartbeat_at,last_idle_probe_at,last_visibility_probe_at,last_egress_used,updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(worker_id) DO UPDATE SET
        need_snapshot=CASE WHEN workers.instance_id<>excluded.instance_id THEN 1 ELSE workers.need_snapshot END,
        instance_id=excluded.instance_id,applied_rev=excluded.applied_rev,code_sha=excluded.code_sha,
        health_ok=excluded.health_ok,active_count=excluded.active_count,queued_count=excluded.queued_count,
        last_heartbeat_at=excluded.last_heartbeat_at,last_idle_probe_at=excluded.last_idle_probe_at,
        last_visibility_probe_at=excluded.last_visibility_probe_at,
        last_egress_used=excluded.last_egress_used,updated_at=excluded.updated_at
      RETURNING need_snapshot,
        (SELECT COUNT(*) FROM commands WHERE worker_id=workers.worker_id AND status='PENDING') AS pending_commands,
        COALESCE((SELECT MAX(revision) FROM pools_configs),0) AS latest_revision
    `).bind(context.workerId, instance, integer(input.rev, "revision"), sha, integer(input.ok, "health", 0, 1),
      integer(input.act, "active count", 0, 100000), integer(input.q, "queued count", 0, 100000),
      now, input.idle_ts == null ? null : Math.min(now, integer(input.idle_ts, "last idle probe")),
      input.probe_ts == null ? null : Math.min(now, integer(input.probe_ts, "last visibility probe")),
      integer(input.eu ?? 0, "egress used"), now).first<{need_snapshot: number; pending_commands: number; latest_revision: number}>();
    return Response.json({ok: true, ...stats, need_snapshot: Boolean(stats?.need_snapshot)});
  });

  router.add("GET", prefix + "/commands", async context => {
    const {results} = await context.env.DB.prepare(`
      SELECT cmd_id,cmd_type,payload_json FROM commands WHERE worker_id=? AND status='PENDING' ORDER BY ordinal LIMIT 50
    `).bind(context.workerId).all<{cmd_id: string; cmd_type: string; payload_json: string}>();
    return Response.json({commands: results.map(({payload_json, ...command}) => ({...command, payload: JSON.parse(payload_json)}))});
  });

  router.add("POST", prefix + "/command-receipts", async context => {
    const data = await body(context);
    const id = string(data.cmd_id, "command", 64, /^[a-zA-Z0-9_-]+$/);
    const status = member(data.status, "status", ["ACKED", "APPLIED", "NOOP", "CONFLICT", "FAILED"] as const);
    const pools = data.pools == null ? null : array(data.pools, "pools", 32).map(value => {
      const row = object(value, "pool receipt");
      return {
        p: string(row.p, "pool", 64, POOL),
        k: member(row.k, "kind", ["train", "npu_smoke"] as const),
        st: member(row.st, "pool status", ["APPLIED", "NOOP", "CONFLICT", "FAILED"] as const),
        c: integer(row.c, "pool count", 0, 100000),
      };
    });
    const result = {rev: nullableInt(data.rev, "revision"), pos: nullableInt(data.pos, "position"),
      ...(pools ? {pools} : {})};
    const error = nullableString(data.err, "error", 200);
    const {DB: db} = context.env;
    const command = await db.prepare("SELECT status,cmd_type,op_id,payload_json FROM commands WHERE cmd_id=? AND worker_id=?")
      .bind(id, context.workerId).first<{status: string; cmd_type: string; op_id: string | null; payload_json: string}>();
    if (!command) throw new HttpError(404, "COMMAND_NOT_FOUND");
    if (command.status === status) return Response.json({ok: true});
    if (command.status !== (status === "ACKED" ? "PENDING" : "ACKED")) {
      throw new HttpError(409, "COMMAND_STATE_CONFLICT");
    }
    const sync = status === "APPLIED" && command.cmd_type === "SYNC_CONFIG";
    if (sync && (result.rev == null || JSON.parse(command.payload_json).revision !== result.rev)) {
      throw new HttpError(400, "COMMAND_REVISION_MISMATCH");
    }
    const changed = await db.batch([
      db.prepare(`UPDATE commands SET status=?,acked_at=COALESCE(acked_at,?),finished_at=?,result_json=?,error_msg=?
        WHERE cmd_id=? AND worker_id=? AND status=?`)
        .bind(status, context.now, status === "ACKED" ? null : context.now, JSON.stringify(result), error,
          id, context.workerId, command.status),
      ...(sync ? [db.prepare(`UPDATE workers SET applied_rev=MAX(applied_rev,?),updated_at=?
        WHERE worker_id=? AND changes()=1`).bind(result.rev, context.now, context.workerId)] : []),
      ...(status !== "ACKED" && command.op_id ? [db.prepare(`UPDATE workflow_priority_ops SET status=(
        CASE
          WHEN EXISTS (SELECT 1 FROM commands WHERE op_id=? AND status IN ('PENDING','ACKED')) THEN 'PENDING'
          WHEN EXISTS (SELECT 1 FROM commands WHERE op_id=? AND status='FAILED') THEN 'FAILED'
          WHEN EXISTS (SELECT 1 FROM commands WHERE op_id=? AND status='CONFLICT') THEN 'CONFLICT'
          WHEN EXISTS (SELECT 1 FROM commands WHERE op_id=? AND status='APPLIED') THEN 'APPLIED'
          ELSE 'NOOP'
        END) WHERE op_id=?`).bind(command.op_id, command.op_id, command.op_id, command.op_id, command.op_id)] : []),
    ]);
    if (changed[0].meta.changes !== 1) throw new HttpError(409, "COMMAND_STATE_CONFLICT");
    return Response.json({ok: true});
  });

  router.add("POST", prefix + "/events", async context => Response.json(await ingestEvents(context, await body(context))));
  router.add("POST", prefix + "/snapshots", async context => Response.json(await ingestSnapshot(context, await body(context))));
  router.add("GET", prefix + "/configs/:revision", async context => {
    const rev = integer(Number(context.params.revision), "revision", 1);
    const row = await context.env.DB.prepare("SELECT revision,sha256,config_json FROM pools_configs WHERE revision=?")
      .bind(rev).first<{revision: number; sha256: string; config_json: string}>();
    if (!row) throw new HttpError(404, "CONFIG_NOT_FOUND");
    const etag = `"${row.sha256}"`;
    if (context.request.headers.get("If-None-Match") === etag) return new Response(null, {status: 304, headers: {ETag: etag}});
    return Response.json({
      revision: row.revision, sha256: row.sha256, config: JSON.parse(row.config_json), config_json: row.config_json,
    }, {headers: {ETag: etag}});
  });
}
