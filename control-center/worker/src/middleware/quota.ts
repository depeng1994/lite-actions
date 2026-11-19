import {type Context, HttpError} from "../router";
import {putMeta} from "../services/retention";

const DAY = 86400;
const exhausted = new WeakMap<D1Database, number>();
const quota = (now: number) => new HttpError(503, "WRITE_QUOTA_EXHAUSTED",
  "D1 每日写入额度已用尽，写操作暂停，CI 正常运行", {"Retry-After": String(DAY - now % DAY)});

export async function requireWriteQuota(context: Context) {
  const day = Math.floor(context.now / DAY);
  const db = context.env.DB;
  const marked = await db.prepare("SELECT meta_value FROM system_meta WHERE meta_key='quota_exhausted_day'")
    .first<string>("meta_value");
  const isoDate = new Date(context.now * 1000).toISOString().slice(0, 10);
  if (exhausted.get(db) === day || (marked !== null && (marked === isoDate || Number(marked) === day))) {
    throw quota(context.now);
  }
}

export async function quotaError(context: Context, error: unknown) {
  if (!/daily row write limit|write quota|D1.*daily write.*(?:quota|limit)/i.test(String(error))) return error;
  const day = Math.floor(context.now / DAY);
  const db = context.env.DB;
  exhausted.set(db, day);
  try {
    await putMeta(db, "quota_exhausted_day", String(day), context.now).run();
  } catch {
    // The provider may already reject every write until midnight.
  }
  return quota(context.now);
}
