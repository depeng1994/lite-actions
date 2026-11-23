#!/usr/bin/env node
import {readFileSync} from "node:fs";
import {DatabaseSync} from "node:sqlite";

const dbPath = process.argv[2];
const inputPath = process.argv[3];
if (!dbPath || !inputPath) throw new Error("Usage: import-public-workflows.mjs <sqlite-path> <json-file>");

const payload = JSON.parse(readFileSync(inputPath, "utf8"));
const runs = [...(payload.active ?? []), ...(payload.history ?? [])];
const db = new DatabaseSync(dbPath);
db.exec("PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;");
const now = Math.floor(Date.now() / 1000);

db.exec("BEGIN IMMEDIATE");
try {
  for (const run of runs) {
    const cases = Array.isArray(run.cases) ? run.cases : [];
    const isActive = Boolean(payload.active?.includes(run));
    const result = run.result === "PASS" ? "PASS" : run.result === "QUEUED" || !run.result ? "QUEUED" : "FAIL";
    const finishedAt = run.finished_at ?? null;
    const maxCards = Number(run.max_cards ?? cases.reduce((sum, item) => sum + Number(item.allocated_cards ?? 0), 0));
    const cardSeconds = Math.round(Number(run.card_hours ?? 0) * 3600);
    const poolsJson = JSON.stringify(run.pools ?? []);
    db.prepare(`INSERT OR IGNORE INTO workflow_runs(
      provider,repo,run_id,attempt,logical_model,submitted_at,workflow_name,workflow_file,pr_number,pr_author,
      trigger_actor,event_type,is_active,has_in_flight,has_waiting_resource_case,active_case_count,total_case_count,
      queue_order_rank,first_started_at,finished_at,result_state,pools_json,max_cards,card_seconds_finished,
      has_unmeasured_allocated,updated_at,display_title
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(
      run.provider, run.repo, run.run_id, run.attempt ?? 1, run.logical_model ?? "unknown", run.submitted_at ?? null,
      run.workflow_name ?? null, run.workflow_file ?? null, run.pr_number ?? null, run.pr_author ?? null,
      run.trigger_actor ?? null, run.event_type ?? null, isActive ? 1 : 0,
      isActive && cases.some(item => ["RUNNING", "RESERVED", "STAGING", "CLEANING"].includes(item.phase)) ? 1 : 0,
      isActive && cases.some(item => item.phase === "QUEUED") ? 1 : 0,
      isActive ? cases.filter(item => item.phase && item.phase !== "QUEUED").length : 0,
      cases.length, null, run.started_at ?? null, finishedAt, result, poolsJson, maxCards, cardSeconds,
      cases.some(item => item.allocated_cards == null) ? 1 : 0, now, run.display_title ?? null,
    );

    for (const item of cases) {
      const taskUid = item.task_uid;
      if (!taskUid || item.finished_at == null) continue;
      const finished = Number(item.finished_at);
      const duration = item.duration_sec == null ? null : Number(item.duration_sec);
      const timingReliable = duration != null ? 1 : 0;
      db.prepare(`INSERT OR IGNORE INTO task_index(
        task_uid,finished_at,worker_id,provider,repo,logical_model,run_id,attempt,dispatch_id,case_id,test_id,label,kind,
        target_pool,req_nodes,req_devices,actual_allocation_json,pr_number,pr_author,trigger_actor,event_type,workflow_file,
        status,exit_code,cleanup_verified,duration_sec,timing_reliable,storage_tier,display_title,submitted_at,run_number
      ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(
        taskUid, finished, item.worker_id ?? "unknown", item.provider ?? run.provider, item.repo ?? run.repo,
        item.logical_model ?? run.logical_model ?? "unknown", item.run_id ?? run.run_id, item.attempt ?? run.attempt ?? 1,
        item.dispatch_id ?? null, item.case_id ?? taskUid, item.test_id ?? item.case_id ?? taskUid, item.label ?? "",
        item.kind ?? "unknown", item.target_pool ?? (run.pools?.[0] ?? "unknown"), item.req_nodes ?? 0, item.req_devices ?? 0,
        JSON.stringify(item.allocation ?? []), item.pr_number ?? run.pr_number ?? null, item.pr_author ?? run.pr_author ?? null,
        item.trigger_actor ?? run.trigger_actor ?? null, item.event_type ?? run.event_type ?? null, item.workflow_file ?? run.workflow_file ?? null,
        item.status ?? result, item.exit_code ?? null, item.cleanup_verified == null ? null : Number(Boolean(item.cleanup_verified)),
        duration, timingReliable, item.storage_tier ?? "hot", run.display_title ?? null, item.submitted_at ?? run.submitted_at ?? null,
        item.run_number ?? run.run_number ?? null,
      );
      db.prepare("INSERT OR IGNORE INTO task_details_hot(task_uid,finished_at,detail_json) VALUES(?,?,?)")
        .run(taskUid, finished, JSON.stringify(item));
      if (duration != null) {
        db.prepare(`INSERT OR IGNORE INTO duration_samples(logical_model,test_id,target_pool,req_nodes,req_devices,duration_sec,finished_at,task_uid)
          VALUES(?,?,?,?,?,?,?,?)`).run(
          item.logical_model ?? run.logical_model ?? "unknown", item.test_id ?? item.case_id ?? taskUid,
          item.target_pool ?? (run.pools?.[0] ?? "unknown"), item.req_nodes ?? 0, item.req_devices ?? 0, duration, finished, taskUid,
        );
      }
    }
  }
  db.exec("COMMIT");
} catch (error) {
  db.exec("ROLLBACK");
  throw error;
} finally {
  db.close();
}
console.log(`Imported ${runs.length} formal workflow summaries into ${dbPath}`);
