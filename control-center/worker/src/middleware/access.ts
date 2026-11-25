import {type Context, HttpError} from "../router";
import {pepper, principal} from "../services/auth";
import {hmac, safeHashEqual} from "../utils/crypto";
import {isTestWorkerId} from "../utils/validate";

const ALLOWED_TEST_PATHS = new Set(["/api/v1/worker/events", "/api/v1/worker/snapshots"]);

export async function authorize(context: Context): Promise<void> {
  const path = context.url.pathname;
  if (path.startsWith("/api/v1/admin/")) {
    const actor = await principal(context);
    if (!actor) throw new HttpError(401, "ADMIN_REQUIRED");
    if (/^\/api\/v1\/admin\/users(?:\/|$)/.test(path) && actor.role !== "super") {
      throw new HttpError(403, "SUPER_REQUIRED");
    }
    context.principal = actor;
  }
  if (path.startsWith("/api/v1/worker/")) {
    const credential = context.request.headers.get("Authorization")?.match(/^Bearer ([!-~]{16,64})$/)?.[1];
    if (!credential) throw new HttpError(401, "WORKER_REQUIRED");
    const hash = await hmac(pepper(context), credential);
    const blue = safeHashEqual(hash, context.env.WORKER_BLUE_KEY_HASH);
    const yellow = safeHashEqual(hash, context.env.WORKER_YELLOW_KEY_HASH);
    if (blue === yellow) throw new HttpError(401, "WORKER_REQUIRED");
    context.workerId = blue ? "blue" : "yellow";
    const declaredWorker = context.request.headers.get("X-Lite-Worker-Id") ?? context.url.searchParams.get("w");
    if (isTestWorkerId(declaredWorker) && !ALLOWED_TEST_PATHS.has(path)) {
      throw new HttpError(403, "TEST_WORKER_FORBIDDEN");
    }
  }
  if (path.startsWith("/api/v1/pr-gate/")) {
    const credential = context.request.headers.get("Authorization")?.match(/^Bearer ([!-~]{16,128})$/)?.[1];
    if (credential && context.env.GATE_COORD_KEY_HASH) {
      const hash = await hmac(pepper(context), credential);
      if (safeHashEqual(hash, context.env.GATE_COORD_KEY_HASH)) {
        context.gateAuthorized = true;
        return;
      }
      throw new HttpError(401, "GATE_AUTH_REQUIRED");
    }
    const actor = await principal(context);
    if (!actor) throw new HttpError(401, "GATE_AUTH_REQUIRED");
    context.principal = actor;
    context.gateAuthorized = true;
  }
}
