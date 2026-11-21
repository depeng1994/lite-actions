import {type Router, body, HttpError} from "../router";
import {auditStatement} from "../services/audit";
import {pepper, superName} from "../services/auth";
import {hmac, randomSecret} from "../utils/crypto";
import {USERNAME} from "./auth";

export function userRoutes(router: Router) {
  router.add("GET", "/api/v1/admin/users", async context => {
    const {results} = await context.env.DB.prepare(
      "SELECT user_id,username,created_at,updated_at FROM users WHERE deleted_at IS NULL ORDER BY username",
    ).all();
    return Response.json({users: results});
  });
  router.add("POST", "/api/v1/admin/users", async context => {
    const {username} = await body(context);
    if (typeof username !== "string" || !USERNAME.test(username) || ["super", superName(context)].includes(username.toLowerCase())) {
      throw new HttpError(400, "INVALID_USERNAME");
    }
    const db = context.env.DB;
    const normalized = username.toLowerCase();
    const id = crypto.randomUUID();
    const secret = randomSecret("la_adm_");
    try {
      await db.batch([
        db.prepare("INSERT INTO users(user_id,username,role,key_hash,created_at,updated_at) VALUES (?,?,'admin',?,?,?)")
          .bind(id, normalized, await hmac(pepper(context), secret), context.now, context.now),
        await auditStatement(context, "CREATE_USER", "user", id, {summary: {username: normalized}}),
      ]);
    } catch (error) {
      if (String(error).includes("UNIQUE constraint failed: users.username")) throw new HttpError(409, "USERNAME_TAKEN");
      throw error;
    }
    return Response.json({user_id: id, username: normalized, secret_once: secret}, {status: 201});
  });
  for (const reset of [true, false]) {
    router.add(reset ? "POST" : "DELETE", `/api/v1/admin/users/:id${reset ? "/reset-key" : ""}`, async context => {
        const db = context.env.DB;
      const id = context.params.id;
      const secret = reset ? randomSecret("la_adm_") : undefined;
      const username = await db.prepare("SELECT username FROM users WHERE user_id=? AND deleted_at IS NULL")
        .bind(id).first<string>("username");
      if (!username) throw new HttpError(404, "USER_NOT_FOUND");
      const changed = await db.batch([
        reset
          ? db.prepare("UPDATE users SET key_hash=?,updated_at=? WHERE user_id=? AND deleted_at IS NULL")
              .bind(await hmac(pepper(context), secret!), context.now, id)
          : db.prepare("UPDATE users SET deleted_at=?,updated_at=? WHERE user_id=? AND deleted_at IS NULL")
              .bind(context.now, context.now, id),
        await auditStatement(context, reset ? "RESET_USER_KEY" : "DELETE_USER", "user", id, {summary: {username}}),
        db.prepare("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL")
          .bind(context.now, id),
      ]);
      if (changed[0].meta.changes !== 1) throw new HttpError(409, "USER_CHANGED");
      return Response.json(reset ? {user_id: id, username, secret_once: secret} : {user_id: id, deleted: true});
    });
  }
  router.add("GET", "/api/v1/admin/audits", async context => {
    const cursor = context.url.searchParams.get("cursor");
    const limit = Number(context.url.searchParams.get("limit") ?? 50);
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 50 || (cursor && !/^\d{1,15}$/.test(cursor))) {
      throw new HttpError(400, "INVALID_CURSOR_OR_LIMIT");
    }
    const {results} = await context.env.DB.prepare(`
      SELECT audit_id,actor_username,actor_role,action,target_type,target_id,summary_json,result,error_msg,created_at
      FROM audit_logs WHERE audit_id < ? ORDER BY audit_id DESC LIMIT ?
    `).bind(cursor ? Number(cursor) : Number.MAX_SAFE_INTEGER, limit + 1)
      .all<{audit_id: number; summary_json: string}>();
    const records = results.slice(0, limit).map(({summary_json, ...row}) => ({...row, summary: JSON.parse(summary_json)}));
    return Response.json({records, next_cursor: results.length > limit ? String(records.at(-1)?.audit_id) : null});
  });
}
