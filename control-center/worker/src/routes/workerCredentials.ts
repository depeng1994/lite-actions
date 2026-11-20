import {body, type Router} from "../router";
import {auditStatement} from "../services/audit";
import {member, string} from "../utils/validate";

const HASH = /^[a-f0-9]{64}$/;

export function workerCredentialRoutes(router: Router) {
  router.add("GET", "/api/v1/admin/worker-credentials", async context => {
    const {results} = await context.env.DB.prepare(
      "SELECT worker_id,scheme,created_at FROM worker_credentials ORDER BY worker_id",
    ).all();
    return Response.json({credentials: results});
  });

  router.add("POST", "/api/v1/admin/worker-credentials", async context => {
    const input = await body(context);
    const workerId = member(input.worker_id, "worker_id", ["blue", "yellow"] as const);
    const scheme = member(input.scheme, "scheme", ["sha256"] as const);
    const hash = string(input.hash, "hash", 64, HASH);
    const createdAt = context.now;
    await context.env.DB.batch([
      context.env.DB.prepare(`INSERT INTO worker_credentials(worker_id,scheme,hash,created_at)
        VALUES (?,?,?,?)
        ON CONFLICT(worker_id) DO UPDATE SET
          scheme=excluded.scheme, hash=excluded.hash, created_at=excluded.created_at`)
        .bind(workerId, scheme, hash, createdAt),
      await auditStatement(context, "REGISTER_WORKER_CREDENTIAL", "worker_credential", workerId, {
        summary: {worker_id: workerId, scheme},
      }),
    ]);
    return Response.json({worker_id: workerId, scheme, created_at: createdAt});
  });
}
