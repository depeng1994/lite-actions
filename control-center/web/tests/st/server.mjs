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

const groups = [[0, 1], [2, 3], [4, 5], [6, 7], [8, 9]];
const host = "192.0.2.1:22";
const yellowHost = "192.0.2.2:22";
const pool = "blue_pool";
const repo = "depeng1994/lite-actions";
const node = (host_id, devices) => ({ host_id, allocation_groups: devices.map((ids) => ({ devices: ids, verified: true })) });
const pools = {
  [pool]: { owner: "blue", enabled: true, type: "ascend-a3", nodes: [node(host, groups), node("192.0.2.3:22", [])] },
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
async function post(worker, route, data) {
  const request = new Request(`http://127.0.0.1/api/v1/worker/${route}`, {
    method: "POST", headers: {Authorization: `Bearer ${key}-${worker}`}, body: JSON.stringify({ w: worker, i: instance, ...data }),
  });
  const response = await handleRequest(request, { ...env, DB }, { now });
  const body = await response.json();
  if (!response.ok || body.ok !== true || (route === "events" && body.need_snapshot)) {
    throw new Error(`Could not seed ${worker}/${route}: ${JSON.stringify(body)}`);
  }
}

function descriptor(index, label, phase, timestamp, devices = [], reserved = timestamp, run = "100") {
  const cid = `case_00${index}_smoke`;
  const tid = createHash("sha256").update(`${repo}:${run}:1:${cid}`).digest("hex").slice(0, 12);
  return {
    t: "TU", tid, p: "github", r: repo, run, att: 1, cid, lbl: label, k: "npu_smoke", pool,
    req: [1, devices.length || 2], ph: phase, ts: timestamp - 1, wn: "Smoke", reliable: 1,
    tm: phase === "QUEUED" ? {} : { [phase]: timestamp, RESERVED: reserved },
    a: devices.length ? [{ h: host, d: devices }] : [],
  };
}

const occupied = (ts, st, d = groups[0]) => ({ t: "IP", pool, h: host, d, st, ts, lo: ts, v: 0, pr: ts });
const queued = descriptor(0, "排期用例", "QUEUED", ago(1));
const waiting = descriptor(1, "等待外部释放用例", "QUEUED", ago(1));
const finished = descriptor(5, "历史用例", "RUNNING", ago(150), [...groups[1], ...groups[2]], ago(151));
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
  { t: "QP", ts: now, pool, k: "npu_smoke", v: 1, rev: 1, last: 1,
    items: [{ tid: queued.tid, pos: 0, ix: [[4]] }, { tid: waiting.tid, pos: 1, ix: [[0]] }] },
  occupied(ago(240), "busy"), occupied(ago(238), "idle"),
  occupied(ago(8), "busy"), occupied(ago(4), "busy"), occupied(now, "busy"),
  occupied(now, "idle", groups[4]),
];
await post("blue", "heartbeat", { rev: 1, ok: 1, act: 3, q: 2 });
for (let i = 0; i < blue.length; i += 6) {
  await post("blue", "events", { ev: blue.slice(i, i + 6).map((event, index) => ({ ...event, s: i + index + 1 })) });
}
await post("yellow", "heartbeat", { rev: 1, ok: 1, act: 0, q: 0 });
await post("yellow", "events", { ev: [{ ...occupied(now, "idle"), h: yellowHost, pool: "yellow_pool", s: 1 }] });
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
