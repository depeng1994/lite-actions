import {type Context, type Principal, HttpError} from "../router";
import {hmac, randomSecret, safeHashEqual, sha256} from "../utils/crypto";

const SESSION_SECONDS = 7 * 86400;

export function superName(context: Context) {
  return (context.env.SUPER_USERNAME || "super").toLowerCase();
}
export function clientIp(context: Context) {
  return context.clientIp ?? "unknown";
}
export function pepper(context: Context) {
  if (!context.env.KEY_PEPPER) throw new HttpError(503, "AUTH_UNAVAILABLE");
  return context.env.KEY_PEPPER;
}
export function cookie(context: Context, token: string, maxAge: number) {
  const secure = context.env.DEV_INSECURE_COOKIE === "1" && context.url.hostname === "localhost" ? "" : "; Secure";
  return `la_session=${token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${maxAge}${secure}`;
}
export async function principal(context: Context): Promise<Principal | undefined> {
  const token = context.request.headers.get("Cookie")?.match(/(?:^|;\s*)la_session=([A-Za-z0-9_-]{43})(?:;|$)/)?.[1];
  if (!token) return undefined;
  const fingerprint = await sha256(token);
  const result = await context.env.DB.prepare(`
    SELECT session_id, s.username, s.role FROM sessions s LEFT JOIN users u ON u.user_id=s.user_id
    WHERE session_id=? AND expires_at>? AND revoked_at IS NULL
      AND (s.role='super' OR (u.user_id IS NOT NULL AND u.deleted_at IS NULL))
  `).bind(fingerprint, context.now).first<Principal>();
  return result ?? undefined;
}

export async function login(context: Context, username: string, secret: string) {
  const {DB: db, SUPER_SECRET: superSecret} = context.env;
  const now = context.now;
  const bucket = await sha256(`${username}|${clientIp(context)}`);
  const locked = await db.prepare("SELECT locked_until FROM login_attempts WHERE bucket_key=?")
    .bind(bucket).first<number>("locked_until");
  if (locked && locked > now) {
    throw new HttpError(429, "LOGIN_LOCKED", "Try again later", {"Retry-After": String(locked - now)});
  }
  const isSuper = username === superName(context);
  const user = isSuper ? null : await db.prepare(
    "SELECT user_id,key_hash FROM users WHERE username=? AND deleted_at IS NULL",
  ).bind(username).first<{user_id: string; key_hash: string}>();
  const key = pepper(context);
  const hash = await hmac(key, secret);
  const expected = isSuper && superSecret ? await hmac(key, superSecret) : user?.key_hash;
  if (!safeHashEqual(hash, expected)) {
    await db.prepare(`
      INSERT INTO login_attempts(bucket_key,fail_count,window_start_at,locked_until) VALUES (?,1,?,NULL)
      ON CONFLICT(bucket_key) DO UPDATE SET
        fail_count=CASE WHEN excluded.window_start_at-window_start_at>=600 THEN 1 ELSE fail_count+1 END,
        window_start_at=CASE WHEN excluded.window_start_at-window_start_at>=600 THEN excluded.window_start_at ELSE window_start_at END,
        locked_until=CASE WHEN excluded.window_start_at-window_start_at<600 AND fail_count>=4 THEN excluded.window_start_at+600 END
        WHERE COALESCE(locked_until,0)<=excluded.window_start_at
    `).bind(bucket, now).run();
    throw new HttpError(401, "INVALID_CREDENTIALS", "Invalid username or key");
  }
  const role = isSuper ? "super" : "admin";
  const token = randomSecret();
  const issued = await db.batch([
    db.prepare(`
      INSERT INTO sessions(session_id,principal_type,user_id,username,role,created_at,expires_at)
      SELECT ?,?,?,?,?,?,? WHERE
        (?='super' OR EXISTS (SELECT 1 FROM users WHERE user_id=? AND deleted_at IS NULL AND key_hash=?))
        AND NOT EXISTS (SELECT 1 FROM login_attempts WHERE bucket_key=? AND locked_until>?)
    `).bind(await sha256(token), role, user?.user_id ?? null, username, role, now, now + SESSION_SECONDS,
      role, user?.user_id ?? null, hash, bucket, now),
    db.prepare("DELETE FROM login_attempts WHERE bucket_key=? AND COALESCE(locked_until,0)<=?").bind(bucket, now),
  ]);
  if (issued[0].meta.changes !== 1) throw new HttpError(401, "INVALID_CREDENTIALS");
  return Response.json({authenticated: true, username, role}, {headers: {"Set-Cookie": cookie(context, token, SESSION_SECONDS)}});
}
