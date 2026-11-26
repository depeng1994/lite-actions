import { createHash, createHmac } from "node:crypto";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";
import { handleRequest } from "../../../worker/dist/app.mjs";
import { startNodeServer } from "../../../worker/src/adapters/nodeServer.ts";
import { NodeSqliteStore } from "../../../worker/src/adapters/nodeSqlite.ts";

const now = Date.parse("2026-11-24T08:00:00Z") / 1000;
const ago = (minutes) => now - minutes * 60;
const directory = mkdtempSync(join(tmpdir(), "lite-actions-st-"));
const dbPath = join(directory, "control-center.sqlite");
const raw = new DatabaseSync(dbPath);
const migrations = new URL("../../../worker/migrations/", import.meta.url);
for (const file of readdirSync(migrations).filter((name) => name.endsWith(".sql")).sort()) {
  raw.exec(readFileSync(new URL(file, migrations), "utf8"));
}

const groups = [[0, 1], [2, 3], [4, 5], [6, 7], [8, 9], [12, 13], [14, 15]];
const host = "192.0.2.1:22";
const blueHost2 = "192.0.2.4:22";
const yellowHost = "192.0.2.2:22";
const pool = "blue_pool";
const repo = "depeng1994/lite-actions";
const node = (host_id, devices) => ({ host_id, allocation_groups: devices.map((ids) => ({ devices: ids, verified: true })) });
const pools = {
  [pool]: { owner: "blue", enabled: true, type: "ascend-a3", nodes: [node(host, groups), node(blueHost2, [[0, 1]]), node("192.0.2.3:22", [])] },
  yellow_pool: { owner: "yellow", enabled: true, type: "ascend-a5", nodes: [node(yellowHost, [[0, 1]])] },
};
raw.prepare("INSERT INTO pools_configs VALUES(1,'fixture',?,'{}','test',?,NULL)").run(JSON.stringify(pools), ago(300));
raw.close();

const DB = new NodeSqliteStore(dbPath);
const key = "browser-fixture-worker-key";
const pepper = "browser-fixture-pepper";
const hash = (worker) => createHmac("sha256", pepper).update(`${key}-${worker}`).digest("hex");
const env = { KEY_PEPPER: pepper, SUPER_SECRET: "browser-fixture-super-secret",
  WORKER_BLUE_KEY_HASH: hash("blue"), WORKER_YELLOW_KEY_HASH: hash("yellow") };
const instance = "abcdef01";
async function postAs(authZone, route, payload) {
  const request = new Request(`http://127.0.0.1/api/v1/worker/${route}`, {
    method: "POST", headers: {Authorization: `Bearer ${key}-${authZone}`}, body: JSON.stringify(payload),
  });
  const response = await handleRequest(request, { ...env, DB }, { now });
  const body = await response.json();
  if (!response.ok || body.ok !== true || (route === "events" && body.need_snapshot)) {
    throw new Error(`Could not seed ${authZone}/${route}: ${JSON.stringify(body)}`);
  }
}
async function post(worker, route, data) {
  return postAs(worker, route, { w: worker, i: instance, ...data });
}

function descriptor(index, label, phase, timestamp, devices = [], reserved = timestamp, run = "100", reporterId = null, allocNodes = null) {
  const cid = `case_00${index}_smoke`;
  const tidSource = reporterId ? `${repo}:${run}:1:${cid}:reporter:${reporterId}` : `${repo}:${run}:1:${cid}`;
  const tid = createHash("sha256").update(tidSource).digest("hex").slice(0, 12);
  const alloc = allocNodes ?? (devices.length ? [{ h: host, d: devices }] : []);
  return {
    t: "TU", tid, p: "github", r: repo, run, att: 1, cid, lbl: label, k: "npu_smoke", pool,
    req: [alloc.length || 1, devices.length || 2], ph: phase, ts: timestamp - 1, wn: "Smoke", reliable: 1,
    tm: phase === "QUEUED" ? {} : { [phase]: timestamp, RESERVED: reserved },
    a: alloc,
  };
}

const occupied = (ts, st, d = groups[0]) => ({ t: "IP", pool, h: host, d, st, ts, lo: ts, v: 0, pr: ts });
const queued = descriptor(0, "排期用例", "QUEUED", ago(1));
const waiting = descriptor(1, "等待外部释放用例", "QUEUED", ago(1));
const finished = descriptor(5, "历史用例", "RUNNING", ago(150), [...groups[1], ...groups[2]], ago(151));
const shortActive = descriptor(6, "零碎秒活跃任务", "RUNNING", now - 40, groups[5], now - 40, "101");
const shortHistory = descriptor(7, "零碎秒归档任务", "RUNNING", ago(180) - 40, groups[5], ago(180) - 40, "102");
const longActive = descriptor(8, "长时零碎秒任务", "RUNNING", now - (150 * 60 + 40), groups[6], now - (150 * 60 + 40), "103");
const samplesEnd = ago(73 * 60);
const samples = Array.from({ length: 5 }, (_, index) => {
  const start = samplesEnd - 1800;
  const sample = descriptor(0, "估时样本", "RUNNING", start, groups[4], start, String(200 + index));
  return [sample, { t: "TF", tid: sample.tid, ts: samplesEnd, st: "PASS", cv: 1 }];
}).flat();
const blue = [
  ...samples,
  queued, waiting, descriptor(2, "运行用例", "RUNNING", ago(35), groups[1], ago(36)),
  descriptor(3, "准备中用例", "STAGING", ago(30), groups[2]),
  descriptor(4, "清理中用例", "CLEANING", ago(25), groups[3]), finished,
  { t: "TF", tid: finished.tid, ts: ago(120), st: "PASS", ec: 0, cv: 1 },
  shortActive, shortHistory,
  { t: "TF", tid: shortHistory.tid, ts: ago(180), st: "PASS", ec: 0, cv: 1 },
  longActive,
  { t: "QP", ts: now, pool, k: "npu_smoke", v: 1, rev: 1, last: 1,
    items: [{ tid: queued.tid, pos: 0, ix: [[4]] }, { tid: waiting.tid, pos: 1, ix: [[0]] }] },
  occupied(ago(240), "busy"), occupied(ago(238), "idle"),
  occupied(ago(8), "busy"), occupied(ago(4), "busy"), occupied(now, "busy"),
  occupied(now, "idle", groups[4]),
];
await post("blue", "heartbeat", { rev: 1, sha: "5905edfdb103a75b4d02e9543e02a0f46ccf2c1b", ok: 1, act: 5, q: 2 });
for (let i = 0; i < blue.length; i += 6) {
  await post("blue", "events", { ev: blue.slice(i, i + 6).map((event, index) => ({ ...event, s: i + index + 1 })) });
}
await post("yellow", "heartbeat", { rev: 1, sha: "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", ok: 1, act: 0, q: 3 });
const allQueued1 = { ...descriptor(0, "全排队用例一", "QUEUED", ago(2), [], ago(2), "105"), pool: "yellow_pool" };
const allQueued2 = { ...descriptor(1, "全排队用例二", "QUEUED", ago(2), [], ago(2), "105"), pool: "yellow_pool" };
const doneAndQueuedDone = { ...descriptor(0, "部分完成已结束用例", "RUNNING", ago(20), [], ago(20), "106"), pool: "yellow_pool" };
const doneAndQueuedWait = { ...descriptor(1, "部分完成排队中用例", "QUEUED", ago(1), [], ago(1), "106"), pool: "yellow_pool" };
await post("yellow", "events", {
  ev: [
    { ...occupied(now, "idle"), h: yellowHost, pool: "yellow_pool", s: 1 },
    { ...allQueued1, s: 2 },
    { ...allQueued2, s: 3 },
    { ...doneAndQueuedDone, s: 4 },
    { t: "TF", tid: doneAndQueuedDone.tid, ts: ago(5), st: "PASS", ec: 0, cv: 1, s: 5 },
    { ...doneAndQueuedWait, s: 6 },
  ],
});

const testWid = "blue-test-id104a1";
const testInst = "bb104001";
await postAs("blue", "test-workers/register", {
  w: testWid, z: "blue", r: repo, pr: 104, sha: "a".repeat(40),
  rid: "104", at: 1, ttl: 3600, exp: now + 3600, i: testInst, st: "ACTIVE",
});
const testSingle = descriptor(4, "已完成测试单卡用例", "RUNNING", ago(45), groups[4], ago(45), "104", testWid);
const testMulti = descriptor(
  5, "已完成测试双机用例", "RUNNING", ago(30), [0, 1], ago(30), "104", testWid,
  [{ h: host, d: groups[6] }, { h: blueHost2, d: [0, 1] }],
);
await postAs("blue", "events", {
  w: testWid, i: testInst,
  ev: [
    { ...testSingle, s: 1 },
    { t: "TL", tid: testSingle.tid, ts: ago(20), ap: 1, a: [], s: 2 },
    { t: "TF", tid: testSingle.tid, ts: ago(20), st: "PASS", ec: 0, cv: 1, a: [], s: 3 },
    { ...testMulti, s: 4 },
    { t: "TL", tid: testMulti.tid, ts: ago(10), ap: 1, a: [], s: 5 },
    { t: "TF", tid: testMulti.tid, ts: ago(10), st: "PASS", ec: 0, cv: 1, a: [], s: 6 },
  ],
});

const activeBlueTestWid = "blue-test-id107a1";
const activeYellowTestWid = "yellow-test-id107a1";
await postAs("blue", "test-workers/register", {
  w: activeBlueTestWid, z: "blue", r: repo, pr: 107, sha: "b".repeat(40),
  rid: "107", at: 1, ttl: 3600, exp: now + 3600, i: "bb107001", st: "ACTIVE",
});
await postAs("yellow", "test-workers/register", {
  w: activeYellowTestWid, z: "yellow", r: repo, pr: 107, sha: "b".repeat(40),
  rid: "107", at: 1, ttl: 3600, exp: now + 3600, i: "cc107001", st: "ACTIVE",
});
const testActiveBlue = descriptor(0, "测试蓝区运行用例", "RUNNING", ago(12), [], ago(12), "107", activeBlueTestWid);
const testActiveYellow = { ...descriptor(1, "测试黄区排队用例", "QUEUED", ago(12), [], ago(12), "107", activeYellowTestWid), pool: "yellow_pool" };
await postAs("blue", "events", { w: activeBlueTestWid, i: "bb107001", ev: [{ ...testActiveBlue, s: 1 }] });
await postAs("yellow", "events", { w: activeYellowTestWid, i: "cc107001", ev: [{ ...testActiveYellow, s: 1 }] });

// Seed an older GitCode test-worker workflow pushed beyond the initial 5 global history rows,
// including a zero-pool worker_probe case and an NPU smoke failure, to cover server-side
// filtered history loading and clean resource-chip rendering.
const gcRepo = "depeng1994/torchtitan-npu";
const gcRunId = "57d83e103f694111be5cf35420780955";
const gcTestWid = "blue-test-id108a1";
const gcInst = "cc108001";
await postAs("blue", "test-workers/register", {
  w: gcTestWid, z: "blue", r: repo, pr: 108, sha: "c".repeat(40),
  rid: "108", at: 1, ttl: 3600, exp: now + 3600, i: gcInst, st: "ACTIVE",
});
const gcTid = (cid) => createHash("sha256")
  .update(`gitcode:${gcRepo}:${gcRunId}:1:${cid}:reporter:${gcTestWid}`).digest("hex").slice(0, 12);
const gcProbeCid = "case_000_worker_os_blue";
const gcSmokeCid = "case_003_dispatch_smoke_16p";
const gcStart = ago(80 * 60);
await postAs("blue", "events", {
  w: gcTestWid, i: gcInst,
  ev: [
    {
      t: "TU", tid: gcTid(gcProbeCid), p: "gitcode", r: gcRepo, run: gcRunId, att: 1, rn: 36, dt: null,
      cid: gcProbeCid, lbl: "Blue OS probe", k: "worker_probe", pool: "", req: [0, 0],
      ph: "RUNNING", ts: gcStart - 10, sub: gcStart - 20, wn: "lite-ci-dispatcher-smoke",
      tr: "depeng1994", et: "Manual", reliable: 1, tm: { RESERVED: gcStart, RUNNING: gcStart }, a: [], s: 1,
    },
    { t: "TF", tid: gcTid(gcProbeCid), ts: gcStart + 10, st: "PASS", ec: 0, cv: 1, a: [], s: 2 },
    {
      t: "TU", tid: gcTid(gcSmokeCid), p: "gitcode", r: gcRepo, run: gcRunId, att: 1, rn: 36, dt: null,
      cid: gcSmokeCid, lbl: "GitCode 16P smoke", k: "npu_smoke", pool, req: [1, 2],
      ph: "RUNNING", ts: gcStart, sub: gcStart - 20, wn: "lite-ci-dispatcher-smoke",
      tr: "depeng1994", et: "Manual", reliable: 1, tm: { RESERVED: gcStart + 15, RUNNING: gcStart + 20 },
      a: [{ h: host, d: groups[4] }], s: 3,
    },
    { t: "TF", tid: gcTid(gcSmokeCid), ts: gcStart + 180, st: "INFRA_ERROR", ec: 1, cv: 1, a: [{ h: host, d: groups[4] }], s: 4 },
  ],
});
DB.close();

const app = startNodeServer({
  dbPath, port: Number(process.env.ST_PORT), host: "127.0.0.1", env,
  staticDir: fileURLToPath(new URL("../../dist/", import.meta.url)),
  handler: (request, runtime, options) => handleRequest(request, runtime, { ...options, now }),
});
await app.ready;
async function stop() {
  await app.close();
  rmSync(directory, { recursive: true, force: true });
}
process.once("SIGINT", stop);
process.once("SIGTERM", stop);
