import {beforeAll, expect, test} from "vitest";
import type {WorkerId} from "../src/types/api";
import {hmac} from "../src/utils/crypto";
import {bindings, db, migrate, request} from "./db";

const now = Math.floor(Date.now() / 1000);
const token = "health-blue-worker-key";
const config = {...bindings, WORKER_BLUE_KEY_HASH: ""};

beforeAll(async () => {
  await migrate();
  config.WORKER_BLUE_KEY_HASH = await hmac(bindings.KEY_PEPPER!, token);
  const pools = {pool_blue: {owner: "blue", enabled: false, type: "ascend-a3", nodes: [{
    host_id: "198.51.100.40:22", allocation_groups: [{devices: [0,1], verified: true}],
  }]}};
  await db.prepare("INSERT INTO pools_configs VALUES(1,'hash',?,'{}','test',?,NULL)").bind(JSON.stringify(pools), now).run();
});

function worker(path: string, data: unknown) {
  return request(`/worker/${path}`, {data, headers: {Authorization: `Bearer ${token}`}}, config);
}

test("IP visibility rounds reach timeline health without changing occupancy", async () => {
  const instance = "feedcafe";
  expect((await worker("heartbeat", {w: "blue", i: instance, rev: 1, sha: "12345678", ok: 1, act: 0, q: 0,
    idle_ts: now, probe_ts: now})).status).toBe(200);
  const base = {w: "blue", i: instance};
  const send = (sequence: number, round: number, visibility: number, failures: number, lastOnline: number | null) =>
    worker("events", {...base, ev: [{s: sequence, t: "IP", pool: "pool_blue", h: "198.51.100.40:22", d: [0,1],
      st: "idle", ts: now, pr: round, v: visibility, mc: failures, lo: lastOnline}]});
  expect((await send(1, 1, 0, 0, now)).status).toBe(200);
  for (let round = 2; round <= 5; round += 1) expect((await send(round, round, 1, round - 1, now)).status).toBe(200);
  expect((await send(6, 6, 2, 5, now)).status).toBe(200);
  expect((await send(7, 6, 2, 5, now)).status).toBe(200);
  expect(await db.prepare("SELECT visibility_state,consecutive_failures,last_online_at,last_probe_round FROM device_group_states").first())
    .toMatchObject({visibility_state: "offline", consecutive_failures: 5, last_online_at: now, last_probe_round: 6});
  const timeline = await (await request("/public/timeline")).json<{workers: Array<{worker_id: WorkerId; pools: Array<{nodes: Array<{device_groups: Array<{health: unknown; state: string}>}>}>}>}>();
  const group = timeline.workers.find(worker => worker.worker_id === "blue")!.pools[0].nodes[0].device_groups[0];
  expect(group).toMatchObject({state: "CONFIRMED_IDLE", health: {status: "OFFLINE", missed_count: 5, last_online_at: now}});
});
