import {type Context, HttpError} from "../router";
import {pepper, principal} from "../services/auth";
import {hmac, safeHashEqual} from "../utils/crypto";

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
  }
}
