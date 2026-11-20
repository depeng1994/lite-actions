import {type Context, HttpError} from "../router";
import {pepper, principal} from "../services/auth";
import type {WorkerId} from "../types/api";
import {hmac, safeHashEqual, sha256} from "../utils/crypto";

export async function authorize(context: Context): Promise<void> {
  const path = context.url.pathname;
  if (path.startsWith("/api/v1/admin/")) {
    const actor = await principal(context);
    if (!actor) throw new HttpError(401, "ADMIN_REQUIRED");
    if (/^\/api\/v1\/admin\/(?:users|worker-credentials)(?:\/|$)/.test(path) && actor.role !== "super") {
      throw new HttpError(403, "SUPER_REQUIRED");
    }
    context.principal = actor;
  }
  if (path.startsWith("/api/v1/worker/")) {
    const credential = context.request.headers.get("Authorization")?.match(/^Bearer ([!-~]{16,64})$/)?.[1];
    if (!credential) throw new HttpError(401, "WORKER_REQUIRED");
    const hmacDigest = await hmac(pepper(context), credential);
    const blue = safeHashEqual(hmacDigest, context.env.WORKER_BLUE_KEY_HASH);
    const yellow = safeHashEqual(hmacDigest, context.env.WORKER_YELLOW_KEY_HASH);
    if (blue && yellow) throw new HttpError(401, "WORKER_REQUIRED");
    if (blue || yellow) {
      context.workerId = blue ? "blue" : "yellow";
      return;
    }
    const digest = await sha256(credential);
    const {results} = await context.env.DB.prepare(
      "SELECT worker_id,hash FROM worker_credentials WHERE scheme='sha256'",
    ).all<{worker_id: WorkerId; hash: string}>();
    let matched: WorkerId | undefined;
    let matches = 0;
    for (const row of results) {
      const rowMatch = safeHashEqual(digest, row.hash);
      matches += rowMatch ? 1 : 0;
      if (rowMatch) matched = row.worker_id;
    }
    if (matches !== 1 || !matched) throw new HttpError(401, "WORKER_REQUIRED");
    context.workerId = matched;
  }
}
