import {modelMap} from "../modelMap";
import {type Context, HttpError} from "../router";
import type {SqlStore} from "../storage/sqlStore";
import type {Cancellation, RunIdentity} from "../types/api";
import {auditStatement} from "./audit";
import {workflowPlatform} from "./cancelPlatform";
import {integer, member, REPO, RUN_ID, string} from "../utils/validate";

type CancelRow = Cancellation & {updated_at: number; requested_by: string};

function lookup(db: SqlStore, id: string) {
  return db.prepare("SELECT * FROM cancellations WHERE cancel_id=?").bind(id).first<CancelRow>();
}
function identityParams(run: RunIdentity) {
  return [run.provider, run.repo, run.run_id, run.attempt];
}
function parse(input: Record<string, unknown>): RunIdentity {
  const provider = member(input.provider, "provider", ["github", "gitcode"] as const);
  return {
    provider, repo: string(input.repo, "repo", 128, REPO).toLowerCase(),
    run_id: string(input.run_id, "run_id", 128, provider === "github" ? /^[1-9][0-9]{0,19}$/ : RUN_ID),
    attempt: integer(input.attempt, "attempt", 1, 2**31 - 1),
  };
}
async function byRun(db: SqlStore, run: RunIdentity) {
  return db.prepare("SELECT * FROM cancellations WHERE provider=? AND repo=? AND run_id=? AND attempt=?")
    .bind(...identityParams(run)).first<CancelRow>();
}

export async function requestCancellation(context: Context, input: Record<string, unknown>) {
  const run = parse(input);
  const {DB: db} = context.env;
  const actor = context.principal!;
  const existing = await byRun(db, run);
  if (existing) return existing;
  const target = `${run.provider}:${run.repo}:${run.run_id}:${run.attempt}`;
  const configured = modelMap.models.some(model => model.repos.some(
    repo => repo.provider === run.provider && repo.repo.toLowerCase() === run.repo,
  ));
  if (!configured) {
    const tracked = await db.prepare(`
      SELECT 1 AS known FROM tasks_active WHERE provider=? AND repo=? COLLATE NOCASE
      UNION ALL SELECT 1 AS known FROM task_index WHERE provider=? AND repo=? COLLATE NOCASE LIMIT 1
    `).bind(run.provider, run.repo, run.provider, run.repo).first("known");
    if (!tracked) throw new HttpError(404, "REPOSITORY_NOT_TRACKED");
  }
  let platform: Awaited<ReturnType<typeof workflowPlatform>>;
  try {
    platform = await workflowPlatform(context.env, run, context.now);
    const state = await platform.state();
    if (state.attempt !== run.attempt) throw new HttpError(409, "ATTEMPT_CHANGED");
    if (state.terminal) throw new HttpError(409, "RUN_ALREADY_ENDED");
  } catch (error) {
    await (await auditStatement(context, "CANCEL_WORKFLOW", "workflow", target,
      {result: "REJECTED", error: error instanceof HttpError ? error.code : "PLATFORM_RUN_UNVERIFIED"})).run();
    throw error;
  }
  const id = "cancel_" + crypto.randomUUID();
  const created = await db.batch([
    db.prepare(`INSERT INTO cancellations(cancel_id,provider,repo,run_id,attempt,status,requested_by,created_at,submitted_at,updated_at)
      VALUES (?,?,?,?,?,'SUBMITTED',?,?,?,?) ON CONFLICT(provider,repo,run_id,attempt) DO NOTHING`)
      .bind(id, ...identityParams(run), actor.username, context.now, context.now, context.now),
    await auditStatement(context, "CANCEL_WORKFLOW", "workflow", target),
  ]);
  if (created[0].meta.changes === 0) return (await byRun(db, run))!;
  try {
    try {
      await platform.stop();
    } catch (err) {
      if (!(err instanceof HttpError) || err.code !== "PLATFORM_UNREACHABLE") throw err;
      const state = await platform.state();
      if (state.attempt !== run.attempt) throw new HttpError(409, "ATTEMPT_CHANGED");
      if (state.terminal) {
        await db.prepare("UPDATE cancellations SET status='PLATFORM_DONE',platform_conclusion=? WHERE cancel_id=?")
          .bind(state.conclusion, id).run();
      } else {
        await platform.stop();
      }
    }
  } catch (error) {
    // If both writes are ambiguous, leave the poller to reconcile from platform state.
    const ambiguous = error instanceof HttpError && error.code === "PLATFORM_UNREACHABLE";
    const code = error instanceof HttpError ? error.message : "PLATFORM_CANCEL_FAILED";
    await db.batch([
      db.prepare("UPDATE cancellations SET status=?,platform_error=?,updated_at=? WHERE cancel_id=?")
        .bind(ambiguous ? "SUBMITTED" : "FAILED", code, context.now, id),
      await auditStatement(context, "CANCEL_WORKFLOW_RESULT", "workflow", target,
        {result: ambiguous ? "UNCERTAIN" : "FAILED", error: code}),
    ]);
  }
  return (await lookup(db, id))!;
}

export async function cancellationDetail(context: Context, id: string) {
  const db = context.env.DB;
  const row = await lookup(db, id);
  if (!row) throw new HttpError(404, "CANCELLATION_NOT_FOUND");
  const result = await db.prepare(`
    SELECT task_uid,worker_id,phase,NULL AS status,NULL AS cleanup_verified,0 AS released FROM tasks_active
    WHERE provider=? AND repo=? COLLATE NOCASE AND run_id=? AND attempt=?
    UNION ALL SELECT task_uid,worker_id,NULL,status,cleanup_verified,COALESCE(cleanup_verified,0) FROM task_index
    WHERE provider=? AND repo=? COLLATE NOCASE AND run_id=? AND attempt=? ORDER BY worker_id,task_uid
  `).bind(...identityParams(row), ...identityParams(row)).all();
  return {...row, cases: result.results};
}

export async function trackCancellations(context: Context) {
  const db = context.env.DB;
  const {results} = await db.prepare(`SELECT * FROM cancellations WHERE status IN ('SUBMITTED','FORCE_CANCEL_PENDING','PLATFORM_DONE')
    ORDER BY updated_at LIMIT 8`).all<CancelRow>();
  for (const row of results) {
    const {cancel_id: id, provider, attempt} = row;
    try {
      if (row.status !== "PLATFORM_DONE") {
        const platform = await workflowPlatform(context.env, row, context.now);
        const state = await platform.state();
        const forceWait = Number(context.env.FORCE_CANCEL_WAIT_SECONDS ?? 120);
        if (state.attempt !== attempt || state.terminal) {
          row.platform_conclusion = state.attempt !== attempt ? "superseded" : state.conclusion;
          row.status = "PLATFORM_DONE";
        } else if (provider === "github" && row.force_cancel_sent_at === null && context.now - (row.submitted_at ?? context.now) >= forceWait) {
          const claim = await db.prepare(`UPDATE cancellations SET status='FORCE_CANCEL_PENDING',force_cancel_sent_at=?,updated_at=?
            WHERE cancel_id=? AND force_cancel_sent_at IS NULL AND status='SUBMITTED'`)
            .bind(context.now, context.now, id).run();
          if (claim.meta.changes === 1) await platform.stop(true);
          continue;
        }
      }
      if (row.status === "PLATFORM_DONE") {
        const counts = await db.prepare(`SELECT
          (SELECT count(*) FROM tasks_active WHERE provider=? AND repo=? COLLATE NOCASE AND run_id=? AND attempt=?) AS running,
          (SELECT count(*) FROM task_index WHERE provider=? AND repo=? COLLATE NOCASE AND run_id=? AND attempt=?
            AND cleanup_verified=0) AS quarantined`).bind(...identityParams(row), ...identityParams(row))
          .first<{running: number; quarantined: number}>();
        if (!counts?.running) row.status = counts?.quarantined ? "COMPLETED_WITH_QUARANTINE" : "COMPLETED";
      }
      await db.prepare(`UPDATE cancellations SET status=?,platform_conclusion=?,platform_error=NULL,updated_at=?
        WHERE cancel_id=? AND status IN ('SUBMITTED','FORCE_CANCEL_PENDING','PLATFORM_DONE')`)
        .bind(row.status, row.platform_conclusion, context.now, id).run();
    } catch (error) {
      await db.prepare("UPDATE cancellations SET platform_error=?,updated_at=? WHERE cancel_id=?")
        .bind(error instanceof HttpError ? error.message : "PLATFORM_STATUS_UNAVAILABLE", context.now, id).run();
    }
  }
}
