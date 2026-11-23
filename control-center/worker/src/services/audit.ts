import type {Context} from "../router";
import {clientIp, pepper} from "./auth";
import {hmac} from "../utils/crypto";

export async function auditStatement(context: Context, action: string, targetType: string, targetId: string, options: {
  summary?: Record<string, unknown>; result?: string; error?: string;
} = {}) {
  const actor = context.principal;
  return context.env.DB.prepare(`
    INSERT INTO audit_logs(actor_username,actor_role,action,target_type,target_id,summary_json,result,error_msg,ip_hash,created_at)
    VALUES (?,?,?,?,?,?,?,?,?,?)
  `).bind(actor?.username ?? "system", actor?.role ?? "system", action, targetType, targetId,
    JSON.stringify(options.summary ?? {}), options.result ?? "OK", options.error ?? null,
    actor ? await hmac(pepper(context), clientIp(context)) : "", context.now);
}
