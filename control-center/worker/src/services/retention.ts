import {type Context} from "../router";
import type {SqlStore} from "../storage/sqlStore";
import {auditStatement} from "./audit";
import {sha256} from "../utils/crypto";
import {blobBuffer, gunzip, gzip} from "../utils/gzip";

const DAY = 86400;
const DEFAULT_CAPACITY = 500 * 1024 * 1024;
const MB = 1024 * 1024;

type ArchiveGroup = {logical_model: string; finish_day: number};
type Detail = {task_uid: string; finished_at: number; detail_json: string};
type SizeProbe = (db: SqlStore) => Promise<number>;
type ColdBlock = {gzip_blob: number[] | ArrayBuffer | ArrayBufferView; sha256: string; record_count: number};

export function putMeta(db: SqlStore, key: string, value: string, now: number) {
  return db.prepare(`INSERT INTO system_meta(meta_key,meta_value,updated_at) VALUES (?,?,?)
    ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value,updated_at=excluded.updated_at`)
    .bind(key, value, now);
}

async function coldGroup(db: SqlStore, group: ArchiveGroup, now: number) {
  const start = group.finish_day * DAY;
  const {results: rows} = await db.prepare(`
      SELECT i.task_uid,i.finished_at,h.detail_json FROM task_index i JOIN task_details_hot h USING (task_uid)
      WHERE i.storage_tier='hot' AND i.logical_model=? AND i.finished_at>=? AND i.finished_at<?
      ORDER BY i.finished_at,i.task_uid LIMIT 100
  `).bind(group.logical_model, start, Math.min(start + DAY, now - 30 * DAY)).all<Detail>();
  if (!rows.length) return;
  const entries: [string, unknown][] = [];
  let rawBytes = 0;
  const encoder = new TextEncoder();
  // A SQLite row including the compressed BLOB must stay under 2 MB.
  for (const row of rows) {
    rawBytes += encoder.encode(row.task_uid + row.detail_json).length + 6;
    if (rawBytes > 1400000) break;
    entries.push([row.task_uid, JSON.parse(row.detail_json)]);
  }
  if (!entries.length) return;
  const text = JSON.stringify(Object.fromEntries(entries));
  const bytes = await gzip(text);
  const sha = await sha256(bytes);
  const chunk = "cold_" + sha;
  await db.prepare(`
    INSERT INTO task_details_cold(chunk_id,logical_model,finish_day,min_finished_at,max_finished_at,record_count,
      sha256,gzip_blob,raw_bytes,compressed_bytes,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(chunk_id) DO NOTHING
  `).bind(chunk, group.logical_model, group.finish_day, rows[0].finished_at, rows[entries.length-1].finished_at,
    entries.length, sha, bytes, encoder.encode(text).length, bytes.byteLength, now).run();
  const stored = await db.prepare("SELECT sha256,gzip_blob,record_count FROM task_details_cold WHERE chunk_id=?")
    .bind(chunk).first<ColdBlock>();
  if (!stored || stored.sha256 !== sha || stored.record_count !== entries.length) throw new Error("COLD_CHUNK_NOT_VERIFIED");
  const blob = blobBuffer(stored.gzip_blob);
  if (await sha256(blob) !== sha || await gunzip(blob) !== text) throw new Error("COLD_CHUNK_NOT_VERIFIED");
  await db.batch([
    db.prepare(`UPDATE task_index SET storage_tier='cold',cold_chunk_id=? WHERE storage_tier='hot'
      AND task_uid IN (SELECT value FROM json_each(?))`).bind(chunk, JSON.stringify(entries.map(([id]) => id))),
    db.prepare("DELETE FROM task_details_hot WHERE task_uid IN (SELECT task_uid FROM task_index WHERE cold_chunk_id=?)").bind(chunk),
  ]);
}

function deleteEmptyChunks(db: SqlStore, now: number) {
  return db.prepare(`DELETE FROM task_details_cold WHERE NOT EXISTS
      (SELECT 1 FROM task_index WHERE cold_chunk_id=task_details_cold.chunk_id) AND created_at<?`).bind(now - 60);
}

function deleteOrphanWorkflowRuns(db: SqlStore) {
  return db.prepare(`DELETE FROM workflow_runs
    WHERE NOT EXISTS (SELECT 1 FROM task_index i WHERE i.provider=workflow_runs.provider
      AND i.repo=workflow_runs.repo AND i.run_id=workflow_runs.run_id AND i.attempt=workflow_runs.attempt)
      AND NOT EXISTS (SELECT 1 FROM tasks_active a WHERE a.provider=workflow_runs.provider
      AND a.repo=workflow_runs.repo AND a.run_id=workflow_runs.run_id AND a.attempt=workflow_runs.attempt)`);
}

function deleteExpiredWorkflows(db: SqlStore, cutoff: number) {
  // Retention is keyed by the complete Workflow. A mixed or partially finished
  // Workflow remains available until every child case has passed the cutoff.
  return db.prepare(`DELETE FROM task_index
    WHERE NOT EXISTS (SELECT 1 FROM tasks_active a WHERE a.provider=task_index.provider
      AND a.repo=task_index.repo AND a.run_id=task_index.run_id AND a.attempt=task_index.attempt)
      AND EXISTS (
        SELECT 1 FROM (
          SELECT provider,repo,run_id,attempt FROM task_index
          WHERE finished_at<? GROUP BY provider,repo,run_id,attempt
          HAVING MAX(finished_at)<?
        ) expired
        WHERE expired.provider=task_index.provider AND expired.repo=task_index.repo
          AND expired.run_id=task_index.run_id AND expired.attempt=task_index.attempt
      )`).bind(cutoff, cutoff);
}

function deleteOldestWorkflow(db: SqlStore) {
  // Select a bounded batch of whole historical Workflows per capacity round.
  // This keeps multi-case runs intact while allowing a 200-row-sized
  // maintenance step when most runs contain one or two cases.
  return db.prepare(`DELETE FROM task_index
    WHERE NOT EXISTS (SELECT 1 FROM tasks_active a WHERE a.provider=task_index.provider
      AND a.repo=task_index.repo AND a.run_id=task_index.run_id AND a.attempt=task_index.attempt)
      AND EXISTS (
        SELECT 1 FROM (
          SELECT provider,repo,run_id,attempt FROM task_index
          WHERE NOT EXISTS (SELECT 1 FROM tasks_active a2 WHERE a2.provider=task_index.provider
            AND a2.repo=task_index.repo AND a2.run_id=task_index.run_id AND a2.attempt=task_index.attempt)
          GROUP BY provider,repo,run_id,attempt
          ORDER BY MAX(finished_at),provider,repo,run_id,attempt LIMIT 200
        ) oldest
        WHERE oldest.provider=task_index.provider AND oldest.repo=task_index.repo
          AND oldest.run_id=task_index.run_id AND oldest.attempt=task_index.attempt
      ) RETURNING finished_at`);
}

export async function archive(context: Context) {
  const {DB: db} = context.env;
  const now = context.now;
  const cutoff = Math.floor((now - 365 * DAY) / DAY) * DAY;
  await db.batch([
    deleteExpiredWorkflows(db, cutoff),
    deleteOrphanWorkflowRuns(db),
    deleteEmptyChunks(db, now),
    db.prepare("DELETE FROM worker_event_dedup WHERE received_at<?").bind(now - 7*DAY),
    db.prepare("DELETE FROM sessions WHERE expires_at<=? OR revoked_at<?").bind(now, now - 7*DAY),
    db.prepare("DELETE FROM snapshot_staging WHERE created_at<?").bind(now - DAY),
    db.prepare("DELETE FROM login_attempts WHERE window_start_at<? AND COALESCE(locked_until,0)<=?").bind(now - DAY, now),
  ]);
  const {results} = await db.prepare(`
    SELECT logical_model,CAST(finished_at/86400 AS INTEGER) AS finish_day FROM task_index
    WHERE storage_tier='hot' AND finished_at<? GROUP BY logical_model,finish_day
    ORDER BY finish_day,logical_model LIMIT 5
  `).bind(now - 30 * DAY).all<ArchiveGroup>();
  for (const group of results) await coldGroup(db, group, now);
}

export async function databaseSize(db: SqlStore): Promise<number> {
  return db.dbSizeBytes();
}

export async function capacity(context: Context, probe: SizeProbe = databaseSize) {
  const db = context.env.DB;
  const limit = Number(context.env.DB_LIMIT_BYTES ?? DEFAULT_CAPACITY);
  if (!Number.isFinite(limit) || limit <= 0) throw new Error("INVALID_DB_LIMIT_BYTES");
  const flags = await db.prepare(`SELECT meta_key,meta_value FROM system_meta WHERE
    meta_key IN ('last_db_size_bytes','capacity_cleanup_pending')`).all<{meta_key: string; meta_value: string}>();
  const meta = Object.fromEntries(flags.results.map(row => [row.meta_key,row.meta_value]));
  let size = await probe(db);
  if (!Number.isFinite(size)) return;
  const pending = meta.capacity_cleanup_pending === "1";
  if (pending ? size < .7 * limit : size <= .8 * limit) {
    if (Math.abs(size - Number(meta.last_db_size_bytes ?? 0)) >= MB || pending) {
      await db.batch([
        putMeta(db, "last_db_size_bytes", String(size), context.now),
        ...(pending ? [putMeta(db, "capacity_cleanup_pending", "0", context.now)] : []),
      ]);
    }
    return;
  }
  let removed = 0;
  let boundary = 0;
  let noHistory = false;
  const startingSize = size;
  for (let round = 0; round < 5 && size >= .7 * limit; round++) {
    // Select oldest first, cascade to hot detail and sample rows, and
    // reclaim cold blocks only when the last referenced summary is gone.
    const results = await db.batch<{finished_at: number}>([
      deleteOldestWorkflow(db),
      deleteOrphanWorkflowRuns(db),
      deleteEmptyChunks(db, context.now),
      db.prepare(`INSERT INTO system_meta(meta_key,meta_value,updated_at)
        SELECT 'capacity_cleanup_pending','1',? WHERE changes()>0
        ON CONFLICT(meta_key) DO UPDATE SET meta_value='1',updated_at=excluded.updated_at`).bind(context.now),
    ]);
    const deleted = results[0].results ?? [];
    if (!deleted.length) {
      noHistory = true;
      break;
    }
    removed += deleted.length;
    boundary = Math.max(boundary, ...deleted.map(row => row.finished_at));
    size = await probe(db);
  }
  if (removed) {
    await db.batch([
      db.prepare(`INSERT INTO system_meta(meta_key,meta_value,updated_at) VALUES ('capacity_purged_before_ts',?,?)
        ON CONFLICT(meta_key) DO UPDATE SET meta_value=MAX(CAST(excluded.meta_value AS INTEGER),CAST(meta_value AS INTEGER)),
          updated_at=excluded.updated_at`).bind(String(boundary), context.now),
      putMeta(db, "last_db_size_bytes", String(size), context.now),
      putMeta(db, "capacity_cleanup_pending", size >= .7*limit && !noHistory ? "1" : "0", context.now),
      await auditStatement(context, "CAPACITY_PURGE", "storage", "DB",
        {summary: {removed, before_bytes: startingSize, after_bytes: size}, result: size >= .7*limit ? "WATERMARK_REMAINS" : "OK"}),
    ]);
  } else if (pending && noHistory) {
    await putMeta(db, "capacity_cleanup_pending", "0", context.now).run();
  }
}

export async function retention(context: Context) {
  await capacity(context);
  await archive(context);
}
