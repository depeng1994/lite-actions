import {DatabaseSync} from "node:sqlite";
import {mkdtempSync, readFileSync, readdirSync, rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {beforeAll, expect, test} from "vitest";
import {runMaintenance} from "../src/app";
import type {ClusterPreview, ClusterSync} from "../src/types/api";
import {hmac, sha256} from "../src/utils/crypto";
import {taskIdentity} from "../src/utils/taskIdentity";
import {bindings, db, migrate, request, sessionCookie, signIn} from "./db";

const blueToken = "iso-blue-worker-key";
const yellowToken = "iso-yellow-worker-key";
const gateToken = "iso-gate-coord-key";
const config = {...bindings};
const now = Math.floor(Date.now() / 1000);
const instMain = "aa110001";
const instBlueTest = "bb220001";
const instYellowTest = "cc330001";

beforeAll(async () => {
  await migrate();
  config.WORKER_BLUE_KEY_HASH = await hmac(bindings.KEY_PEPPER!, blueToken);
  config.WORKER_YELLOW_KEY_HASH = await hmac(bindings.KEY_PEPPER!, yellowToken);
  config.GATE_COORD_KEY_HASH = await hmac(bindings.KEY_PEPPER!, gateToken);
});

function workerReq(
  zone: "blue" | "yellow",
  path: string,
  data?: unknown,
  extraHeaders: Record<string, string> = {},
  reqNow = now,
) {
  return request(
    "/worker/" + path,
    {
      data,
      headers: {
        Authorization: "Bearer " + (zone === "blue" ? blueToken : yellowToken),
        ...extraHeaders,
      },
      now: reqNow,
    },
    config,
  );
}

async function registerTestWorker(
  zone: "blue" | "yellow",
  workerId: string,
  runId: string,
  attempt: number,
  instanceId: string,
  ttl = 3600,
  pr = 52,
) {
  return workerReq(zone, "test-workers/register", {
    w: workerId,
    z: zone,
    r: "depeng1994/lite-actions",
    pr,
    sha: "a".repeat(40),
    rid: runId,
    at: attempt,
    ttl,
    exp: now + ttl,
    i: instanceId,
    st: "ACTIVE",
  });
}

async function taskSpec(
  opts: {
    repo?: string;
    runId: string;
    attempt?: number;
    caseId: string;
    kind?: string;
    pool?: string;
    reporterId?: string;
    phase?: string;
    alloc?: Array<{h: string; d: number[]}>;
  },
) {
  const attempt = opts.attempt ?? 1;
  const repo = opts.repo ?? "depeng1994/lite-actions";
  const ids = await taskIdentity(
    {provider: "github", repo, run_id: opts.runId, attempt},
    opts.caseId,
    null,
    opts.reporterId,
  );
  return {
    ids,
    payload: {
      tid: ids.short_tid,
      p: "github",
      r: repo,
      run: opts.runId,
      att: attempt,
      cid: opts.caseId,
      lbl: `Smoke ${opts.caseId}`,
      k: opts.kind ?? "npu_smoke",
      pool: opts.pool ?? "a3_shared_blue",
      req: [1, 2],
      ph: opts.phase ?? "QUEUED",
      ts: now - 20,
      sub: now - 30,
      wn: "PR Hardware Smoke",
      rn: 12,
      dt: `PR #52 ${opts.runId}`,
      tm: {QUEUED: now - 20},
      reliable: 1,
      ...(opts.alloc ? {a: opts.alloc} : {}),
    },
  };
}

test("migrations 0001-0012 preserve foreign keys on hot details/samples and backfill reporter_id", async () => {
  const tmpDir = mkdtempSync(join(tmpdir(), "cc-mig-0012-"));
  try {
    const raw = new DatabaseSync(join(tmpDir, "mig.sqlite"));
    raw.exec("PRAGMA foreign_keys = ON;");
    const files = readdirSync(new URL("../migrations/", import.meta.url))
      .filter(name => name.endsWith(".sql"))
      .sort();
    for (const file of files) {
      if (file >= "0012_") break;
      raw.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), "utf8"));
    }
    raw.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,
      run_id,attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,updated_at)
      VALUES('t_old_active','oldact000001','repo:r0:1:case_000_a','blue','github','org/repo','model','r0',1,
        'case_000_a','t0','L0','npu_smoke','pool_a',1,2,'QUEUED',1,100)`).run();
    raw.prepare(`INSERT INTO task_index(task_uid,finished_at,worker_id,provider,repo,logical_model,
      run_id,attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable)
      VALUES('t_old_idx',200,'blue','github','org/repo','model','r0_done',1,
        'case_000_b','t1','L1','npu_smoke','pool_a',1,2,'[]','PASS',1)`).run();
    raw.prepare(`INSERT INTO task_details_hot(task_uid,finished_at,detail_json) VALUES('t_old_idx',200,'{}')`).run();
    raw.prepare(`INSERT INTO duration_samples(logical_model,test_id,target_pool,req_nodes,req_devices,duration_sec,finished_at,task_uid)
      VALUES('model','t1','pool_a',1,2,30,200,'t_old_idx')`).run();
    raw.prepare(`INSERT INTO workflow_runs(provider,repo,run_id,attempt,logical_model,is_active,has_in_flight,
      has_waiting_resource_case,active_case_count,total_case_count,result_state,pools_json,max_cards,card_seconds_finished,
      has_unmeasured_allocated,updated_at)
      VALUES('github','org/repo','r0_done',1,'model',0,0,0,0,1,'PASS','["pool_a"]',2,60,0,200)`).run();

    raw.exec(readFileSync(new URL("../migrations/0012_test_workers.sql", import.meta.url), "utf8"));
    const fkErrors = raw.prepare("PRAGMA foreign_key_check").all();
    expect(fkErrors).toEqual([]);
    const activeRow = raw.prepare("SELECT reporter_id,is_test FROM tasks_active WHERE task_uid='t_old_active'").get() as {
      reporter_id: string; is_test: number;
    };
    expect(activeRow).toEqual({reporter_id: "blue", is_test: 0});
    const idxRow = raw.prepare("SELECT reporter_id,is_test FROM task_index WHERE task_uid='t_old_idx'").get() as {
      reporter_id: string; is_test: number;
    };
    expect(idxRow).toEqual({reporter_id: "blue", is_test: 0});
    expect(raw.prepare("SELECT count(*) AS n FROM task_details_hot WHERE task_uid='t_old_idx'").get()).toEqual({n: 1});
    expect(raw.prepare("SELECT count(*) AS n FROM duration_samples WHERE task_uid='t_old_idx'").get()).toEqual({n: 1});
    const wfRow = raw.prepare("SELECT is_test,reporter_ids_json FROM workflow_runs WHERE run_id='r0_done'").get() as {
      is_test: number; reporter_ids_json: string;
    };
    expect(wfRow.is_test).toBe(0);
    expect(JSON.parse(wfRow.reporter_ids_json)).toEqual(["blue"]);
    raw.close();
  } finally {
    rmSync(tmpDir, {recursive: true, force: true});
  }
});

test("test worker endpoints enforce registration, zone ownership, and control-endpoint bans", async () => {
  expect((await registerTestWorker("yellow", "blue-test-id100a1", "100", 1, instBlueTest)).status).toBe(403);
  expect((await workerReq("blue", "events", {w: "blue-test-id100a1", i: instBlueTest, ev: []})).status).toBe(403);
  expect((await registerTestWorker("blue", "blue-test-id100a1", "100", 1, instBlueTest)).status).toBe(200);
  expect((await registerTestWorker("yellow", "yellow-test-id100a1", "100", 1, instYellowTest)).status).toBe(200);

  expect((await workerReq("blue", "heartbeat", {
    w: "blue-test-id100a1", i: instBlueTest, rev: 1, sha: "1234", ok: 1, act: 0, q: 0,
  })).status).toBe(403);
  expect((await workerReq("blue", "commands", undefined, {"X-Lite-Worker-Id": "blue-test-id100a1"})).status).toBe(403);
  expect((await workerReq("blue", "commands?w=blue-test-id100a1")).status).toBe(403);
  expect((await workerReq("blue", "command-receipts", {
    w: "blue-test-id100a1", cmd_id: "c1", status: "ACKED",
  })).status).toBe(403);
  expect((await workerReq("blue", "configs/1?w=blue-test-id100a1")).status).toBe(403);
  expect((await workerReq("blue", "configs/1", undefined, {"X-Lite-Worker-Id": "blue-test-id100a1"})).status).toBe(403);

  expect((await workerReq("blue", "events", {
    w: "blue-test-id100a1", i: instBlueTest,
    ev: [{s: 1, t: "IP", pool: "a3_shared_blue", h: "198.51.100.1:22", d: [0, 1], st: "idle", ts: now}],
  })).status).toBe(403);
  expect((await workerReq("blue", "snapshots", {
    w: "blue-test-id100a1", i: instBlueTest, sid: "aabbccdd01", seq: 1, n: 1, p: 1, kind: "device",
    items: [{pool: "a3_shared_blue", h: "198.51.100.1:22", d: [0, 1], st: "idle", ts: now}],
  })).status).toBe(403);

  expect((await workerReq("yellow", "test-workers/deregister", {w: "blue-test-id100a1"})).status).toBe(403);
  await registerTestWorker("blue", "blue-test-id101a1", "101", 1, instBlueTest);
  expect((await workerReq("blue", "test-workers/deregister", {w: "blue-test-id101a1", reason: "done"})).status).toBe(200);
  expect((await workerReq("blue", "events", {w: "blue-test-id101a1", i: instBlueTest, ev: []})).status).toBe(403);
});

test("dual-zone test aggregation, cross-reporter non-hijack, duration_samples, and MOVE_UP isolation", async () => {
  await workerReq("blue", "heartbeat", {w: "blue", i: instMain, rev: 1, sha: "1234", ok: 1, act: 0, q: 0});

  const prod = await taskSpec({runId: "100", caseId: "case_000_shared", pool: "a3_shared_blue"});
  const blueTest = await taskSpec({
    runId: "100", caseId: "case_000_shared", pool: "a3_shared_blue", reporterId: "blue-test-id100a1",
  });
  const yellowTest = await taskSpec({
    runId: "100", caseId: "case_001_yellow", pool: "a3_shared_yellow", reporterId: "yellow-test-id100a1",
  });
  expect(prod.ids.task_uid).not.toBe(blueTest.ids.task_uid);

  expect((await workerReq("blue", "events", {
    w: "blue", i: instMain,
    ev: [{s: 1, t: "TU", ...prod.payload}, {s: 2, t: "TP", tid: prod.ids.short_tid, ph: "QUEUED", ts: now - 10}],
  })).status).toBe(200);
  expect((await workerReq("blue", "events", {
    w: "blue-test-id100a1", i: instBlueTest,
    ev: [
      {s: 1, t: "TU", ...blueTest.payload},
      {s: 2, t: "TP", tid: blueTest.ids.short_tid, ph: "RUNNING", ts: now - 10},
      {s: 3, t: "TF", tid: blueTest.ids.short_tid, ts: now, st: "PASS", ec: 0, cv: 1},
    ],
  })).status).toBe(200);
  expect((await workerReq("yellow", "events", {
    w: "yellow-test-id100a1", i: instYellowTest,
    ev: [{s: 1, t: "TU", ...yellowTest.payload}],
  })).status).toBe(200);

  // Main task remains QUEUED and untouched; Test duration excluded from duration_samples.
  expect(await db.prepare("SELECT phase FROM tasks_active WHERE task_uid=?")
    .bind(prod.ids.task_uid).first("phase")).toBe("QUEUED");
  expect(await db.prepare("SELECT count(*) AS n FROM duration_samples WHERE task_uid=?")
    .bind(blueTest.ids.task_uid).first("n")).toBe(0);

  // One shared (provider, repo, run_id, attempt, is_test=1) row merges both zones, separate from is_test=0.
  const wfRows = await db.prepare(`
    SELECT is_test,reporter_ids_json,is_active,total_case_count
    FROM workflow_runs WHERE repo='depeng1994/lite-actions' AND run_id='100' AND attempt=1
    ORDER BY is_test
  `).all<{is_test: number; reporter_ids_json: string; is_active: number; total_case_count: number}>();
  expect(wfRows.results).toHaveLength(2);
  expect(wfRows.results[0]).toMatchObject({is_test: 0, is_active: 1, total_case_count: 1});
  expect(JSON.parse(wfRows.results[0].reporter_ids_json)).toEqual(["blue"]);
  expect(wfRows.results[1]).toMatchObject({is_test: 1, is_active: 1, total_case_count: 2});
  expect(JSON.parse(wfRows.results[1].reporter_ids_json).sort()).toEqual(["blue-test-id100a1", "yellow-test-id100a1"]);

  // Second test-only workflow cannot be expedited via MOVE_UP.
  await registerTestWorker("blue", "blue-test-id102a1", "102", 1, "dd440001");
  const testOnly = await taskSpec({
    runId: "102", caseId: "case_000_q", pool: "a3_shared_blue", reporterId: "blue-test-id102a1",
  });
  expect((await workerReq("blue", "events", {
    w: "blue-test-id102a1", i: "dd440001", ev: [{s: 1, t: "TU", ...testOnly.payload}],
  })).status).toBe(200);

  const publicWf = await (await request("/public/workflows?history_limit=10")).json<{
    active: Array<{run_id: string; is_test: boolean; can_move_up: boolean; result: string; reporter_ids: string[]}>;
  }>();
  const prodRun100 = publicWf.active.find(r => r.run_id === "100" && !r.is_test)!;
  expect(prodRun100.result).toBe("QUEUED");
  const testRun100 = publicWf.active.find(r => r.run_id === "100" && r.is_test)!;
  expect(testRun100.can_move_up).toBe(false);
  expect(testRun100.result).toBe("RUNNING");
  expect(testRun100.reporter_ids.sort()).toEqual(["blue-test-id100a1", "yellow-test-id100a1"]);
  const testRun102 = publicWf.active.find(r => r.run_id === "102" && r.is_test)!;
  expect(testRun102.result).toBe("QUEUED");

  const cookie = sessionCookie(await signIn());
  const moveResp = await request("/admin/workflows/move-up", {
    cookie,
    data: {provider: "github", repo: "depeng1994/lite-actions", run_id: "102", attempt: 1, queue_version: 0},
  }, config);
  expect(moveResp.status).toBe(409);
  expect(await moveResp.json()).toMatchObject({code: "TEST_WORKFLOW_PRIORITY_LOCKED"});
});

test("retention reaper expires blue test worker without clobbering active yellow sibling", async () => {
  const bExp = "blue-test-id200a1";
  const yLive = "yellow-test-id200a1";
  expect((await registerTestWorker("blue", bExp, "200", 1, "ee550001", 600)).status).toBe(200);
  expect((await registerTestWorker("yellow", yLive, "200", 1, "ff660001", 7200)).status).toBe(200);

  const bTask = await taskSpec({runId: "200", caseId: "case_000_b", pool: "a3_shared_blue", reporterId: bExp});
  const yTask = await taskSpec({runId: "200", caseId: "case_001_y", pool: "a3_shared_yellow", reporterId: yLive});
  expect((await workerReq("blue", "events", {
    w: bExp, i: "ee550001",
    ev: [{s: 1, t: "TU", ...bTask.payload}, {s: 2, t: "TP", tid: bTask.ids.short_tid, ph: "RUNNING", ts: now - 5}],
  })).status).toBe(200);
  expect((await workerReq("yellow", "events", {
    w: yLive, i: "ff660001",
    ev: [{s: 1, t: "TU", ...yTask.payload}, {s: 2, t: "TP", tid: yTask.ids.short_tid, ph: "RUNNING", ts: now - 5}],
  })).status).toBe(200);

  await runMaintenance("retention", config, now + 1000);

  expect(await db.prepare("SELECT status FROM test_workers WHERE worker_id=?").bind(bExp).first("status")).toBe("STOPPED");
  expect(await db.prepare("SELECT status FROM test_workers WHERE worker_id=?").bind(yLive).first("status")).toBe("ACTIVE");
  expect(await db.prepare("SELECT status FROM task_index WHERE task_uid=?").bind(bTask.ids.task_uid).first("status")).toBe("INFRA_ERROR");
  expect(await db.prepare("SELECT phase FROM tasks_active WHERE task_uid=?").bind(yTask.ids.task_uid).first("phase")).toBe("RUNNING");
  expect(await db.prepare("SELECT is_active,active_case_count FROM workflow_runs WHERE run_id='200' AND is_test=1").first())
    .toMatchObject({is_active: 1, active_case_count: 1});
});

test("cluster drain and delete inspect test worker leases while excluding test keys from SYNC_CONFIG", async () => {
  await db.batch([
    db.prepare("DELETE FROM queue_orders"),
    db.prepare("DELETE FROM tasks_active"),
  ]);
  const host = "198.51.100.77:22";
  const clusterCfg = {
    a3_shared_blue: {
      owner: "blue",
      type: "ascend-a3",
      enabled: true,
      process_indexing: "card-die",
      nodes: [{
        host_id: host,
        ssh: host,
        node_ip: "192.0.2.77",
        network_interface: "eth0",
        machine_fingerprint: "c".repeat(64),
        device_ids: [0, 1],
        allocation_groups: [{devices: [0, 1], verified: true}],
      }],
    },
  };
  const cfgText = JSON.stringify(clusterCfg);
  const rev = ((await db.prepare("SELECT MAX(revision) AS r FROM pools_configs").first<number>("r")) ?? 0) + 1;
  await db.prepare("INSERT INTO pools_configs VALUES(?,?,?,'{}','seed',?,NULL)")
    .bind(rev, await sha256(cfgText), cfgText, now).run();

  const bActive = "blue-test-id300a1";
  await registerTestWorker("blue", bActive, "300", 1, "aa770001", 3600);
  const prodQueued = await taskSpec({runId: "301", caseId: "case_000_prod_q", pool: "a3_shared_blue"});
  const testLeased = await taskSpec({
    runId: "300", caseId: "case_000_test_run", pool: "a3_shared_blue", reporterId: bActive,
    phase: "RUNNING", alloc: [{h: host, d: [0, 1]}],
  });
  const testQueued = await taskSpec({
    runId: "300", caseId: "case_001_test_q", pool: "a3_shared_blue", reporterId: bActive, phase: "QUEUED",
  });
  expect((await workerReq("blue", "events", {w: "blue", i: instMain, ev: [{s: 3, t: "TU", ...prodQueued.payload}]})).status).toBe(200);
  expect((await workerReq("blue", "events", {
    w: bActive, i: "aa770001",
    ev: [{s: 1, t: "TU", ...testLeased.payload}, {s: 2, t: "TU", ...testQueued.payload}],
  })).status).toBe(200);

  const cookie = sessionCookie(await signIn());
  const delPreview = await (await request("/admin/cluster/preview", {
    cookie,
    data: {base_revision: rev, operation: {op: "DELETE_NODE", pool: "a3_shared_blue", host_id: host}},
  }, config)).json<ClusterPreview>();
  expect(delPreview.blocking_reasons).toContain("ACTIVE_LEASES_REMAINING");
  expect(delPreview.blocking_reasons).toContain("QUEUED_TASKS_REMAINING");

  const drainOp = {op: "DRAIN", scope: "node", pool: "a3_shared_blue", host_id: host};
  const drainPreview = await (await request("/admin/cluster/preview", {
    cookie,
    data: {base_revision: rev, operation: drainOp},
  }, config)).json<ClusterPreview>();
  expect(drainPreview.drain_cancel_tasks).toContain(prodQueued.ids.task_uid);
  expect(drainPreview.drain_cancel_tasks).toContain(testQueued.ids.task_uid);

  const drained = await (await request("/admin/cluster/publish", {
    cookie,
    data: {base_revision: rev, operation: drainOp, preview_hash: drainPreview.preview_hash},
  }, config)).json<ClusterSync>();
  const syncPayload = await db.prepare("SELECT payload_json FROM commands WHERE cmd_id=?")
    .bind(drained.command_ids[0]).first<string>("payload_json");
  const drainKeys = JSON.parse(syncPayload!).drain_cancel_keys as string[];
  expect(drainKeys).toContain(prodQueued.ids.task_key);
  expect(drainKeys).not.toContain(testQueued.ids.task_key);
});

test("deregistering blue test worker closes only its active tasks and rolls up mixed workflow runs", async () => {
  await db.batch([
    db.prepare("DELETE FROM queue_orders"),
    db.prepare("DELETE FROM tasks_active"),
  ]);
  const mainInst = "aa110002";
  const bTest = "blue-test-id45542029869a1";
  const yTest = "yellow-test-id45542029869a1";
  const bTestInst = "bb455401";
  const yTestInst = "cc455401";
  const runId = "45542029869";
  const host = "198.51.100.77:22";

  await workerReq("blue", "heartbeat", {w: "blue", i: mainInst, rev: 1, sha: "1234", ok: 1, act: 1, q: 0});
  expect((await registerTestWorker("blue", bTest, runId, 1, bTestInst, 3600, 56)).status).toBe(200);
  expect((await registerTestWorker("yellow", yTest, runId, 1, yTestInst, 3600, 56)).status).toBe(200);

  const prodTask = await taskSpec({runId, caseId: "case_000_prod_live", pool: "a3_shared_blue", phase: "RUNNING"});
  const deployBlue = await taskSpec({
    runId, caseId: "case_000_deploy_blue", kind: "test_worker_deploy", pool: "a3_shared_blue",
  });
  const cleanupBlue = await taskSpec({
    runId, caseId: "case_999_cleanup_blue", kind: "test_worker_cleanup", pool: "a3_shared_blue",
  });
  const bluePassed = await taskSpec({
    runId, caseId: "case_001_passed", pool: "a3_shared_blue", reporterId: bTest,
  });
  const blueStuck = await taskSpec({
    runId, caseId: "case_005_dispatch_smoke_16p", pool: "a3_shared_blue", reporterId: bTest,
    phase: "RUNNING", alloc: [{h: host, d: [0, 1]}],
  });
  const blueQueued = await taskSpec({
    runId, caseId: "case_006_queued_smoke", pool: "a3_shared_blue", reporterId: bTest, phase: "QUEUED",
  });
  const yellowRunning = await taskSpec({
    runId, caseId: "case_007_yellow_smoke", pool: "a3_shared_yellow", reporterId: yTest, phase: "RUNNING",
  });

  expect((await workerReq("blue", "events", {
    w: "blue", i: mainInst,
    ev: [
      {s: 1, t: "TU", ...prodTask.payload},
      {s: 2, t: "TU", ...deployBlue.payload},
      {s: 3, t: "TF", tid: deployBlue.ids.short_tid, ts: now - 15, st: "PASS", ec: 0, cv: 1},
      {s: 4, t: "TU", ...cleanupBlue.payload},
      {s: 5, t: "TP", tid: cleanupBlue.ids.short_tid, ph: "RUNNING", ts: now - 5},
    ],
  })).status).toBe(200);
  expect((await workerReq("blue", "events", {
    w: bTest, i: bTestInst,
    ev: [
      {s: 1, t: "TU", ...bluePassed.payload},
      {s: 2, t: "TF", tid: bluePassed.ids.short_tid, ts: now - 12, st: "PASS", ec: 0, cv: 1},
      {s: 3, t: "TU", ...blueStuck.payload},
      {s: 4, t: "TP", tid: blueStuck.ids.short_tid, ph: "RUNNING", ts: now - 10},
      {s: 5, t: "TU", ...blueQueued.payload},
      {s: 6, t: "QP", ts: now - 9, pool: "a3_shared_blue", k: "npu_smoke", v: 1, rev: 1, last: 1,
        items: [{tid: blueQueued.ids.short_tid, pos: 0, ix: [[0]]}]},
    ],
  })).status).toBe(200);
  expect((await workerReq("yellow", "events", {
    w: yTest, i: yTestInst,
    ev: [{s: 1, t: "TU", ...yellowRunning.payload}, {s: 2, t: "TP", tid: yellowRunning.ids.short_tid, ph: "RUNNING", ts: now - 8}],
  })).status).toBe(200);

  const deregResp = await workerReq("blue", "test-workers/deregister", {w: bTest, reason: "cleanup"}, {}, now + 30);
  expect(deregResp.status).toBe(200);
  expect(await deregResp.json()).toEqual({ok: true, worker_id: bTest, finalized_tasks: 2});

  expect(await db.prepare("SELECT status,deregistered_at FROM test_workers WHERE worker_id=?").bind(bTest).first())
    .toEqual({status: "STOPPED", deregistered_at: now + 30});
  expect(await db.prepare("SELECT count(*) AS n FROM tasks_active WHERE reporter_id=?").bind(bTest).first("n")).toBe(0);
  expect(await db.prepare("SELECT count(*) AS n FROM queue_orders WHERE task_uid=?").bind(blueQueued.ids.task_uid).first("n")).toBe(0);

  // Main worker prod task, main worker cleanup task, and yellow test task stay untouched.
  expect(await db.prepare("SELECT phase FROM tasks_active WHERE task_uid=?").bind(prodTask.ids.task_uid).first("phase")).toBe("RUNNING");
  expect(await db.prepare("SELECT phase FROM tasks_active WHERE task_uid=?").bind(cleanupBlue.ids.task_uid).first("phase")).toBe("RUNNING");
  expect(await db.prepare("SELECT phase FROM tasks_active WHERE task_uid=?").bind(yellowRunning.ids.task_uid).first("phase")).toBe("RUNNING");

  const stuckArchived = await db.prepare(
    "SELECT status,exit_code,finished_at,started_at,actual_allocation_json FROM task_index WHERE task_uid=?",
  ).bind(blueStuck.ids.task_uid).first<{status: string; exit_code: number; finished_at: number; started_at: number; actual_allocation_json: string}>();
  expect(stuckArchived).toMatchObject({
    status: "CANCELLED",
    exit_code: 130,
    finished_at: now + 30,
    started_at: now - 10,
  });
  expect(JSON.parse(stuckArchived!.actual_allocation_json)).toEqual([{host_id: host, devices: [0, 1]}]);
  const stuckDetail = JSON.parse(String(
    await db.prepare("SELECT detail_json FROM task_details_hot WHERE task_uid=?").bind(blueStuck.ids.task_uid).first("detail_json"),
  ));
  expect(stuckDetail).toMatchObject({
    queued_at: now - 20,
    running_at: now - 10,
    reconciled: 1,
    termination_reason: "test_worker_deregistered:cleanup",
  });

  // Finish cleanup (FAIL) and deregister yellow test worker -> mixed workflow transitions to finished FAIL.
  expect((await workerReq("blue", "events", {
    w: "blue", i: mainInst,
    ev: [{s: 6, t: "TF", tid: cleanupBlue.ids.short_tid, ts: now + 35, st: "FAIL", ec: 1, cv: 0}],
  }, {}, now + 35)).status).toBe(200);
  expect((await workerReq("yellow", "test-workers/deregister", {w: yTest, reason: "cleanup"}, {}, now + 40)).status).toBe(200);

  expect(await db.prepare(`
    SELECT is_active,has_in_flight,has_waiting_resource_case,active_case_count,total_case_count,finished_at,result_state
    FROM workflow_runs WHERE provider='github' AND repo='depeng1994/lite-actions' AND run_id=? AND attempt=1 AND is_test=1
  `).bind(runId).first()).toEqual({
    is_active: 0,
    has_in_flight: 0,
    has_waiting_resource_case: 0,
    active_case_count: 0,
    total_case_count: 6,
    finished_at: now + 40,
    result_state: "FAIL",
  });
  expect(await db.prepare(`
    SELECT is_active,result_state FROM workflow_runs
    WHERE provider='github' AND repo='depeng1994/lite-actions' AND run_id=? AND attempt=1 AND is_test=0
  `).bind(runId).first()).toEqual({is_active: 1, result_state: "QUEUED"});

  const board = await (await request("/public/workflows?history_limit=20&repo=depeng1994%2Flite-actions")).json<{
    active: Array<{run_id: string; is_test: boolean}>;
    history: Array<{run_id: string; is_test: boolean; result: string}>;
  }>();
  expect(board.active.some(r => r.run_id === runId && r.is_test)).toBe(false);
  expect(board.active.some(r => r.run_id === runId && !r.is_test)).toBe(true);
  expect(board.history.find(r => r.run_id === runId && r.is_test)).toMatchObject({result: "FAIL"});

  const taskView = await (await request(`/public/tasks/${blueStuck.ids.task_uid}`)).json<{
    status: string; exit_code: number; running_at: number;
  }>();
  expect(taskView).toMatchObject({status: "CANCELLED", exit_code: 130, running_at: now - 10});
});

test("reapExpiredTestWorkers reconciles pre-existing STOPPED test workers and missing-worker orphans without touching prod or live yellow", async () => {
  await db.batch([
    db.prepare("DELETE FROM queue_orders"),
    db.prepare("DELETE FROM tasks_active"),
    db.prepare("DELETE FROM device_occupancy_intervals"),
  ]);
  const stoppedWorker = "blue-test-id45542029870a1";
  const missingWorker = "blue-test-id45542029879a1";
  const liveYellow = "yellow-test-id45542029871a1";
  const runStopped = "45542029870";
  const runMissing = "45542029879";
  const runLiveYellow = "45542029871";

  await registerTestWorker("blue", stoppedWorker, runStopped, 1, "bb700001", 7200, 56);
  await db.prepare("UPDATE test_workers SET status='STOPPED',deregistered_at=?,updated_at=? WHERE worker_id=?")
    .bind(now - 1800, now - 1800, stoppedWorker).run();
  await registerTestWorker("yellow", liveYellow, runLiveYellow, 1, "cc710001", 7200, 58);

  const stuckIds = await taskIdentity({provider: "github", repo: "depeng1994/lite-actions", run_id: runStopped, attempt: 1}, "case_005_dispatch_smoke_16p", null, stoppedWorker);
  const missingIds = await taskIdentity({provider: "github", repo: "depeng1994/lite-actions", run_id: runMissing, attempt: 1}, "case_002_orphan_q", null, missingWorker);
  const yellowIds = await taskIdentity({provider: "github", repo: "depeng1994/lite-actions", run_id: runLiveYellow, attempt: 1}, "case_003_live_y", null, liveYellow);
  const prodIds = await taskIdentity({provider: "github", repo: "depeng1994/lite-actions", run_id: runStopped, attempt: 1}, "case_000_prod", null);

  await db.batch([
    db.prepare(`INSERT INTO task_index(task_uid,finished_at,worker_id,reporter_id,is_test,provider,repo,logical_model,
      run_id,attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable,submitted_at)
      VALUES(?,?, 'blue','blue',1,'github','depeng1994/lite-actions','lite-actions',?,1,
        'case_999_cleanup_blue','cleanup_blue','Cleanup blue','test_worker_cleanup','a3_shared_blue',0,0,'[]','PASS',1,?)`)
      .bind("cleanup-stopped-uid", now - 1700, runStopped, now - 2000),
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,reporter_id,is_test,provider,repo,logical_model,
      run_id,attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,allocation_json,queued_at,running_at,timing_reliable,updated_at)
      VALUES(?,?,?,?,?,1,'github','depeng1994/lite-actions','lite-actions',?,1,'case_005_dispatch_smoke_16p','dispatch_smoke_16p',
        'Dispatch 16p','npu_smoke','a3_shared_blue',2,8,'RUNNING','[]',?,?,1,?)`)
      .bind(stuckIds.task_uid, stuckIds.short_tid, stuckIds.task_key, "blue", stoppedWorker, runStopped, now - 2200, now - 2132, now - 2132),
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,reporter_id,is_test,provider,repo,logical_model,
      run_id,attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,queued_at,timing_reliable,updated_at)
      VALUES(?,?,?,?,?,1,'github','depeng1994/lite-actions','lite-actions',?,1,'case_002_orphan_q','orphan_q',
        'Orphan Q','npu_smoke','a3_shared_blue',1,2,'QUEUED',?,1,?)`)
      .bind(missingIds.task_uid, missingIds.short_tid, missingIds.task_key, "blue", missingWorker, runMissing, now - 1900, now - 1900),
    db.prepare(`INSERT INTO queue_orders(worker_id,pool_name,kind,task_uid,queue_pos,candidate_group_idxs_json,queue_ver,updated_at)
      VALUES('blue','a3_shared_blue','npu_smoke',?,0,'{"rev":1,"options":[[0]]}',1,?)`)
      .bind(missingIds.task_uid, now - 1900),
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,reporter_id,is_test,provider,repo,logical_model,
      run_id,attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,running_at,timing_reliable,updated_at)
      VALUES(?,?,?,?,?,1,'github','depeng1994/lite-actions','lite-actions',?,1,'case_003_live_y','live_y',
        'Live Y','npu_smoke','a3_shared_yellow',1,2,'RUNNING',?,1,?)`)
      .bind(yellowIds.task_uid, yellowIds.short_tid, yellowIds.task_key, "yellow", liveYellow, runLiveYellow, now - 50, now - 50),
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,reporter_id,is_test,provider,repo,logical_model,
      run_id,attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,running_at,timing_reliable,updated_at)
      VALUES(?,?,?,?,?,0,'github','depeng1994/lite-actions','lite-actions',?,1,'case_000_prod','prod',
        'Prod run','npu_smoke','a3_shared_blue',1,2,'RUNNING',?,1,?)`)
      .bind(prodIds.task_uid, prodIds.short_tid, prodIds.task_key, "blue", "blue", runStopped, now - 50, now - 50),
    db.prepare(`INSERT INTO device_occupancy_intervals(worker_id,pool_name,host_id,devices_json,started_at,last_busy_at)
      VALUES('blue','a3_shared_blue','198.51.100.77:22','[0,1]',?,?)`).bind(now - 500, now - 10),
    db.prepare(`INSERT INTO device_occupancy_intervals(worker_id,pool_name,host_id,devices_json,started_at,last_busy_at)
      VALUES(?,'a3_shared_blue','198.51.100.77:22','[2,3]',?,?)`).bind(stoppedWorker, now - 500, now - 10),
    db.prepare(`INSERT INTO workflow_runs(provider,repo,run_id,attempt,is_test,reporter_ids_json,logical_model,
      is_active,has_in_flight,has_waiting_resource_case,active_case_count,total_case_count,result_state,pools_json,
      max_cards,card_seconds_finished,has_unmeasured_allocated,updated_at)
      VALUES('github','depeng1994/lite-actions',?,1,1,?,'lite-actions',1,1,0,1,2,'QUEUED','["a3_shared_blue"]',16,0,0,?)`)
      .bind(runStopped, JSON.stringify(["blue", stoppedWorker]), now - 1700),
    db.prepare(`INSERT INTO workflow_runs(provider,repo,run_id,attempt,is_test,reporter_ids_json,logical_model,
      is_active,has_in_flight,has_waiting_resource_case,active_case_count,total_case_count,result_state,pools_json,
      max_cards,card_seconds_finished,has_unmeasured_allocated,updated_at)
      VALUES('github','depeng1994/lite-actions',?,1,1,?,'lite-actions',1,0,1,1,1,'QUEUED','["a3_shared_blue"]',2,0,0,?)`)
      .bind(runMissing, JSON.stringify([missingWorker]), now - 1900),
  ]);

  await runMaintenance("retention", config, now);

  expect(await db.prepare("SELECT status,exit_code,started_at FROM task_index WHERE task_uid=?").bind(stuckIds.task_uid).first())
    .toEqual({status: "CANCELLED", exit_code: 130, started_at: now - 2132});
  expect(await db.prepare("SELECT status,exit_code FROM task_index WHERE task_uid=?").bind(missingIds.task_uid).first())
    .toEqual({status: "CANCELLED", exit_code: 130});
  expect(await db.prepare("SELECT count(*) AS n FROM tasks_active WHERE task_uid IN (?,?)")
    .bind(stuckIds.task_uid, missingIds.task_uid).first("n")).toBe(0);
  expect(await db.prepare("SELECT count(*) AS n FROM queue_orders WHERE task_uid=?").bind(missingIds.task_uid).first("n")).toBe(0);
  expect(await db.prepare("SELECT is_active,has_in_flight,active_case_count,finished_at,result_state FROM workflow_runs WHERE run_id=? AND is_test=1")
    .bind(runStopped).first()).toEqual({is_active: 0, has_in_flight: 0, active_case_count: 0, finished_at: now, result_state: "FAIL"});
  expect(await db.prepare("SELECT is_active,result_state,finished_at FROM workflow_runs WHERE run_id=? AND is_test=1")
    .bind(runMissing).first()).toEqual({is_active: 0, result_state: "FAIL", finished_at: now});

  // Production occupancy & active task plus live yellow test task remain completely intact.
  expect(await db.prepare("SELECT count(*) AS n FROM device_occupancy_intervals WHERE worker_id='blue'").first("n")).toBe(1);
  expect(await db.prepare("SELECT count(*) AS n FROM device_occupancy_intervals WHERE worker_id=?").bind(stoppedWorker).first("n")).toBe(0);
  expect(await db.prepare("SELECT phase FROM tasks_active WHERE task_uid=?").bind(prodIds.task_uid).first("phase")).toBe("RUNNING");
  expect(await db.prepare("SELECT phase FROM tasks_active WHERE task_uid=?").bind(yellowIds.task_uid).first("phase")).toBe("RUNNING");
});

test("cleanup TF and terminal pr-gate report close dangling zone test workers cleanly", async () => {
  const mainInst = "aa110003";
  const runId = "45542029872";
  const bTest = `blue-test-id${runId}a1`;
  const yTest = `yellow-test-id${runId}a1`;
  const sha = "b".repeat(40);

  await workerReq("blue", "heartbeat", {w: "blue", i: mainInst, rev: 1, sha: "1234", ok: 1, act: 0, q: 0});
  expect((await request("/pr-gate/register", {
    data: {
      repo: "depeng1994/lite-actions", pr_number: 57, pr_head_sha: sha,
      github_run_id: runId, github_run_attempt: 1,
    },
    headers: {Authorization: "Bearer " + gateToken},
    now,
  }, config)).status).toBe(200);

  expect((await workerReq("blue", "test-workers/register", {
    w: bTest, z: "blue", r: "depeng1994/lite-actions", pr: 57, sha, rid: runId, at: 1, ttl: 3600, exp: now + 3600, i: "bb872001", st: "ACTIVE",
  })).status).toBe(200);
  expect((await workerReq("yellow", "test-workers/register", {
    w: yTest, z: "yellow", r: "depeng1994/lite-actions", pr: 57, sha, rid: runId, at: 1, ttl: 3600, exp: now + 3600, i: "cc872001", st: "ACTIVE",
  })).status).toBe(200);

  const bTask = await taskSpec({runId, caseId: "case_005_blue_run", pool: "a3_shared_blue", reporterId: bTest, phase: "RUNNING"});
  const yTask = await taskSpec({runId, caseId: "case_006_yellow_run", pool: "a3_shared_yellow", reporterId: yTest, phase: "RUNNING"});
  const bCleanup = await taskSpec({runId, caseId: "case_999_cleanup_blue", kind: "test_worker_cleanup", pool: "a3_shared_blue"});

  expect((await workerReq("blue", "events", {
    w: bTest, i: "bb872001",
    ev: [{s: 1, t: "TU", ...bTask.payload}, {s: 2, t: "TP", tid: bTask.ids.short_tid, ph: "RUNNING", ts: now - 5}],
  })).status).toBe(200);
  expect((await workerReq("yellow", "events", {
    w: yTest, i: "cc872001",
    ev: [{s: 1, t: "TU", ...yTask.payload}, {s: 2, t: "TP", tid: yTask.ids.short_tid, ph: "RUNNING", ts: now - 5}],
  })).status).toBe(200);

  // Even when cleanup fails without calling test-workers/deregister, blue's test worker is closed while yellow stays active.
  expect((await workerReq("blue", "events", {
    w: "blue", i: mainInst,
    ev: [
      {s: 1, t: "TU", ...bCleanup.payload},
      {s: 2, t: "TF", tid: bCleanup.ids.short_tid, ts: now + 10, st: "INFRA_ERROR", ec: 125, cv: 0},
    ],
  }, {}, now + 10)).status).toBe(200);

  expect(await db.prepare("SELECT status FROM test_workers WHERE worker_id=?").bind(bTest).first("status")).toBe("STOPPED");
  expect(await db.prepare("SELECT status FROM task_index WHERE task_uid=?").bind(bTask.ids.task_uid).first("status")).toBe("CANCELLED");
  expect(await db.prepare("SELECT status FROM test_workers WHERE worker_id=?").bind(yTest).first("status")).toBe("ACTIVE");
  expect(await db.prepare("SELECT phase FROM tasks_active WHERE task_uid=?").bind(yTask.ids.task_uid).first("phase")).toBe("RUNNING");

  // Terminal PR gate report closes the remaining dangling yellow test worker and finalizes workflow_runs.
  expect((await request("/pr-gate/report", {
    data: {
      repo: "depeng1994/lite-actions", pr_number: 57, pr_head_sha: sha, generation: 1,
      status: "CANCELLED", github_run_id: runId, github_run_attempt: 1,
    },
    headers: {Authorization: "Bearer " + gateToken},
    now: now + 20,
  }, config)).status).toBe(200);

  expect(await db.prepare("SELECT status FROM test_workers WHERE worker_id=?").bind(yTest).first("status")).toBe("STOPPED");
  expect(await db.prepare("SELECT status,exit_code FROM task_index WHERE task_uid=?").bind(yTask.ids.task_uid).first())
    .toEqual({status: "CANCELLED", exit_code: 130});
  expect(await db.prepare("SELECT is_active,result_state FROM workflow_runs WHERE run_id=? AND is_test=1").bind(runId).first())
    .toEqual({is_active: 0, result_state: "FAIL"});
});

test("completed test tasks preserve multi-node allocations on timeline and workflows even if terminal events omit or empty allocation", async () => {
  await db.batch([
    db.prepare("DELETE FROM queue_orders"),
    db.prepare("DELETE FROM tasks_active"),
    db.prepare("DELETE FROM device_occupancy_intervals"),
  ]);
  const blue153 = "113.46.17.205:153";
  const blue154 = "113.46.17.205:154";
  const yellow22 = "7.246.124.101:22";
  const clusterCfg = {
    a3_shared_blue: {
      owner: "blue",
      type: "ascend-a3",
      enabled: true,
      process_indexing: "card-die",
      nodes: [
        {host_id: blue153, ssh: blue153, node_ip: "113.46.17.205", network_interface: "eth0",
          machine_fingerprint: "a".repeat(64), device_ids: Array.from({length: 16}, (_, i) => i),
          allocation_groups: [{devices: [0,1,2,3,4,5,6,7], verified: true}, {devices: [8,9,10,11,12,13,14,15], verified: true}]},
        {host_id: blue154, ssh: blue154, node_ip: "113.46.17.205", network_interface: "eth1",
          machine_fingerprint: "b".repeat(64), device_ids: Array.from({length: 16}, (_, i) => i),
          allocation_groups: [{devices: [0,1,2,3,4,5,6,7], verified: true}, {devices: [8,9,10,11,12,13,14,15], verified: true}]},
      ],
    },
    a3_shared_pool: {
      owner: "yellow",
      type: "ascend-a3",
      enabled: true,
      process_indexing: "card-die",
      nodes: [
        {host_id: yellow22, ssh: yellow22, node_ip: "7.246.124.101", network_interface: "eth0",
          machine_fingerprint: "c".repeat(64), device_ids: Array.from({length: 8}, (_, i) => i),
          allocation_groups: [{devices: [0,1,2,3,4,5,6,7], verified: true}]},
      ],
    },
  };
  const cfgText = JSON.stringify(clusterCfg);
  const rev = ((await db.prepare("SELECT MAX(revision) AS r FROM pools_configs").first<number>("r")) ?? 0) + 1;
  await db.prepare("INSERT INTO pools_configs VALUES(?,?,?,'{}','seed',?,NULL)")
    .bind(rev, await sha256(cfgText), cfgText, now).run();
  expect((await workerReq("blue", "heartbeat", {w: "blue", i: instMain, rev, sha: "1234", ok: 1, act: 0, q: 0})).status).toBe(200);
  expect((await workerReq("yellow", "heartbeat", {w: "yellow", i: "dd110001", rev, sha: "1234", ok: 1, act: 0, q: 0})).status).toBe(200);

  const runId = "45547843931";
  const bTest = "blue-test-id45547843931a1";
  const yTest = "yellow-test-id45547843931a1";
  await registerTestWorker("blue", bTest, runId, 1, "bb990001", 3600, 61);
  await registerTestWorker("yellow", yTest, runId, 1, "cc990001", 3600, 61);

  const bCase004 = await taskSpec({runId, caseId: "case_004_dispatch_smoke_8p", pool: "a3_shared_blue", reporterId: bTest});
  const bCase005 = await taskSpec({runId, caseId: "case_005_dispatch_smoke_16p", pool: "a3_shared_blue", reporterId: bTest});
  bCase005.payload.req = [2, 8];
  const bRequeue = await taskSpec({runId, caseId: "case_009_requeue_test", pool: "a3_shared_blue", reporterId: bTest});
  const yCase007 = await taskSpec({runId, caseId: "case_007_dispatch_smoke_yellow_a3_8p", pool: "a3_shared_pool", reporterId: yTest});

  // Blue case 004 & 005 run with allocations, then receive a stale TL(a=[]), empty-lease snapshot, and TF(a=[]).
  expect((await workerReq("blue", "events", {
    w: bTest, i: "bb990001",
    ev: [
      {s: 1, t: "TU", ...bCase004.payload},
      {s: 2, t: "TP", tid: bCase004.ids.short_tid, ph: "RUNNING", ts: now - 120},
      {s: 3, t: "TL", tid: bCase004.ids.short_tid, ts: now - 120, ap: 1, a: [{h: blue153, d: [0,1,2,3,4,5,6,7]}]},
      {s: 4, t: "TU", ...bCase005.payload},
      {s: 5, t: "TP", tid: bCase005.ids.short_tid, ph: "RUNNING", ts: now - 110},
      {s: 6, t: "TL", tid: bCase005.ids.short_tid, ts: now - 110, ap: 1, a: [{h: blue153, d: [8,9,10,11,12,13,14,15]}, {h: blue154, d: [0,1,2,3,4,5,6,7]}]},
    ],
  }, {}, now - 100)).status).toBe(200);

  // Snapshot rebase with empty lease for CLEANING tasks preserves prior allocation_json.
  expect((await workerReq("blue", "snapshots", {
    w: bTest, i: "bb990001", sid: "abcd123456", seq: 6, p: 1, n: 1, kind: "task",
    items: [
      {...bCase004.payload, ph: "CLEANING", tm: {QUEUED: now - 130, RUNNING: now - 120, CLEANING: now - 20}},
      {...bCase005.payload, ph: "CLEANING", tm: {QUEUED: now - 130, RUNNING: now - 110, CLEANING: now - 15}},
    ],
  }, {}, now - 15)).status).toBe(200);

  // Stale TL(a=[]) and TF(a=[]) during CLEANING/REPORTED still preserve prior allocation_json!
  expect((await workerReq("blue", "events", {
    w: bTest, i: "bb990001",
    ev: [
      {s: 7, t: "TL", tid: bCase004.ids.short_tid, ts: now - 10, ap: 1, a: []},
      {s: 8, t: "TF", tid: bCase004.ids.short_tid, ts: now - 10, st: "PASS", ec: 0, cv: 1, a: []},
      {s: 9, t: "TL", tid: bCase005.ids.short_tid, ts: now - 5, ap: 1, a: []},
      {s: 10, t: "TF", tid: bCase005.ids.short_tid, ts: now - 5, st: "PASS", ec: 0, cv: 1, a: []},
    ],
  }, {}, now)).status).toBe(200);

  // Requeue to QUEUED + TL(a=[]) genuinely clears allocation_json.
  expect((await workerReq("blue", "events", {
    w: bTest, i: "bb990001",
    ev: [
      {s: 11, t: "TU", ...bRequeue.payload},
      {s: 12, t: "TP", tid: bRequeue.ids.short_tid, ph: "RESERVED", ts: now - 4},
      {s: 13, t: "TL", tid: bRequeue.ids.short_tid, ts: now - 4, ap: 1, a: [{h: blue154, d: [8,9,10,11,12,13,14,15]}]},
      {s: 14, t: "TP", tid: bRequeue.ids.short_tid, ph: "QUEUED", ts: now - 3},
      {s: 15, t: "TL", tid: bRequeue.ids.short_tid, ts: now - 3, ap: 1, a: []},
    ],
  }, {}, now)).status).toBe(200);
  expect(await db.prepare("SELECT allocation_json FROM tasks_active WHERE task_uid=?").bind(bRequeue.ids.task_uid).first("allocation_json")).toBe("[]");
  expect((await workerReq("blue", "events", {
    w: bTest, i: "bb990001",
    ev: [{s: 16, t: "TF", tid: bRequeue.ids.short_tid, ts: now - 2, st: "CANCELLED", ec: 130, cv: 1}],
  }, {}, now)).status).toBe(200);

  // Yellow case 007 completes with allocation preserved.
  expect((await workerReq("yellow", "events", {
    w: yTest, i: "cc990001",
    ev: [
      {s: 1, t: "TU", ...yCase007.payload},
      {s: 2, t: "TP", tid: yCase007.ids.short_tid, ph: "RUNNING", ts: now - 90},
      {s: 3, t: "TL", tid: yCase007.ids.short_tid, ts: now - 90, ap: 1, a: [{h: yellow22, d: [0,1,2,3,4,5,6,7]}]},
      {s: 4, t: "TF", tid: yCase007.ids.short_tid, ts: now - 5, st: "PASS", ec: 0, cv: 1},
    ],
  }, {}, now)).status).toBe(200);

  const tlResp = await (await request("/public/timeline?history_hours=72&compact_history=1", {now})).json<{
    history_task_uids?: string[];
    tasks_by_uid: Record<string, {task_uid: string; is_test: boolean; allocation: Array<{devices: number[]}>}>;
    workers: Array<{worker_id: string; pools: Array<{pool_name: string; nodes: Array<{device_groups: Array<{devices: number[]; solid_blocks: Array<{task_uid: string}>}>}>}>}>;
  }>();
  expect(tlResp.history_task_uids).toContain(bCase004.ids.task_uid);
  expect(tlResp.history_task_uids).toContain(bCase005.ids.task_uid);
  expect(tlResp.history_task_uids).toContain(yCase007.ids.task_uid);
  expect(tlResp.tasks_by_uid[bCase005.ids.task_uid].allocation).toHaveLength(2);

  const blueNodes = tlResp.workers.find(w => w.worker_id === "blue")!.pools.find(p => p.pool_name === "a3_shared_blue")!.nodes;
  expect(blueNodes[0].device_groups[0].solid_blocks.some(b => b.task_uid === bCase004.ids.task_uid)).toBe(true);
  expect(blueNodes[0].device_groups[1].solid_blocks.some(b => b.task_uid === bCase005.ids.task_uid)).toBe(true);
  expect(blueNodes[1].device_groups[0].solid_blocks.some(b => b.task_uid === bCase005.ids.task_uid)).toBe(true);
  const yellowNodes = tlResp.workers.find(w => w.worker_id === "yellow")!.pools.find(p => p.pool_name === "a3_shared_pool")!.nodes;
  expect(yellowNodes[0].device_groups[0].solid_blocks.some(b => b.task_uid === yCase007.ids.task_uid)).toBe(true);

  const wfResp = await (await request("/public/workflows?history_limit=10&repo=depeng1994%2Flite-actions", {now})).json<{
    history: Array<{run_id: string; is_test: boolean; cases: Array<{task_uid: string; allocated_cards: number; allocation: Array<{display_host_id: string; devices: number[]}>}>}>;
  }>();
  const wf = wfResp.history.find(r => r.run_id === runId && r.is_test)!;
  const c005 = wf.cases.find(c => c.task_uid === bCase005.ids.task_uid)!;
  expect(c005.allocated_cards).toBe(16);
  expect(c005.allocation.map(a => a.display_host_id)).toEqual(["113.xxx.xxx.205:153", "113.xxx.xxx.205:154"]);
  const c004 = wf.cases.find(c => c.task_uid === bCase004.ids.task_uid)!;
  expect(c004.allocation.map(a => a.display_host_id)).toEqual(["113.xxx.xxx.205:153"]);
});
