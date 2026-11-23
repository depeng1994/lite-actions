import {beforeAll, expect, test} from "vitest";
import {db, migrate, request} from "./db";

const now = Math.floor(Date.now() / 1000);

beforeAll(async () => {
  await migrate();
  const config = {pool_blue: {owner: "blue", enabled: true, type: "ascend-a3", nodes: [{
    host_id: "198.51.100.30:22", allocation_groups: [{devices: [0,1,2,3,4,5,6,7], verified: true}],
  }]}};
  await db.batch([
    db.prepare("INSERT INTO pools_configs VALUES(1,'hash',?,'{}','test',?,NULL)").bind(JSON.stringify(config), now),
    db.prepare(`INSERT INTO workers(worker_id,instance_id,applied_rev,code_sha,health_ok,active_count,queued_count,
      last_heartbeat_at,last_idle_probe_at,last_egress_used,updated_at)
      VALUES('blue','01234567',1,'sha',1,0,1,?,?,0,?)`).bind(now, now, now),
    db.prepare(`INSERT INTO device_group_states(worker_id,pool_name,host_id,devices_json,occupancy_state,probe_at,
      visibility_state,consecutive_failures,last_online_at,last_probe_round,updated_at)
      VALUES('blue','pool_blue','198.51.100.30:22','[0,1,2,3,4,5,6,7]','idle',?,'online',0,?,?,?)`).bind(now, now, 1, now),
    ...[1,3,5,7,9].map((duration, index) => db.prepare(`INSERT INTO task_index(
      task_uid,finished_at,worker_id,provider,repo,logical_model,run_id,attempt,case_id,test_id,label,kind,
      target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable,duration_sec)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).bind(
      `sample-${index}`, now - index, "blue", "github", "org/schedule", "schedule", `sample-run-${index}`, 1,
      `case_00${index}_sample`, "npu_smoke", "Sample", "npu_smoke", "pool_blue", 1, 8, "[]", "PASS", 1, duration,
    )),
    ...[1,3,5,7,9].map((duration, index) => db.prepare(`INSERT INTO duration_samples(
      logical_model,test_id,target_pool,req_nodes,req_devices,duration_sec,finished_at,task_uid)
      VALUES('schedule','npu_smoke','pool_blue',1,8,?,?,?)`).bind(duration, now - index, `sample-${index}`)),
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,queued_at,updated_at)
      VALUES('schedule-task','schedule-task','schedule-key','blue','github','org/schedule','schedule','schedule-run',1,
      'case_000_scheduled','npu_smoke','Scheduled','npu_smoke','pool_blue',1,8,'QUEUED',1,?,?)`).bind(now, now),
    db.prepare(`INSERT INTO queue_orders(worker_id,pool_name,kind,task_uid,queue_pos,candidate_group_idxs_json,queue_ver,updated_at)
      VALUES('blue','pool_blue','npu_smoke','schedule-task',0,'{"rev":1,"options":[[0]]}',1,?)`).bind(now),
  ]);
});

test("timeline API projects the median duration from reliable samples", async () => {
  const timeline = await (await request("/public/timeline")).json<{
    projections: Array<{task_uid: string; projection_status: string; est_start?: number; est_end?: number}>;
  }>();
  const projection = timeline.projections.find(({task_uid}) => task_uid === "schedule-task");
  expect(projection).toMatchObject({task_uid: "schedule-task", projection_status: "SCHEDULED", uncertain_release: false});
  const estStart = projection?.est_start;
  const estEnd = projection?.est_end;
  expect(estStart).toBeGreaterThanOrEqual(now);
  expect(estStart).toBeLessThanOrEqual(now + 1);
  expect(estEnd).toBeDefined();
  if (estStart === undefined || estEnd === undefined) throw new Error("missing schedule projection timestamps");
  expect(estEnd).toBe(estStart + 5);
});
