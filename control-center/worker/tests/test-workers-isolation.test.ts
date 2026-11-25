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
const config = {...bindings};
const now = Math.floor(Date.now() / 1000);
const instMain = "aa110001";
const instBlueTest = "bb220001";
const instYellowTest = "cc330001";

beforeAll(async () => {
  await migrate();
  config.WORKER_BLUE_KEY_HASH = await hmac(bindings.KEY_PEPPER!, blueToken);
  config.WORKER_YELLOW_KEY_HASH = await hmac(bindings.KEY_PEPPER!, yellowToken);
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
) {
  return workerReq(zone, "test-workers/register", {
    w: workerId,
    z: zone,
    r: "depeng1994/lite-actions",
    pr: 52,
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
    active: Array<{run_id: string; is_test: boolean; can_move_up: boolean; reporter_ids: string[]}>;
  }>();
  const testRun100 = publicWf.active.find(r => r.run_id === "100" && r.is_test)!;
  expect(testRun100.can_move_up).toBe(false);
  expect(testRun100.reporter_ids.sort()).toEqual(["blue-test-id100a1", "yellow-test-id100a1"]);

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
