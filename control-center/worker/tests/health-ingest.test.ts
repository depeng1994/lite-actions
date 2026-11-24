import {beforeAll, beforeEach, expect, test} from "vitest";
import type {Timeline, WorkerId} from "../src/types/api";
import {hmac} from "../src/utils/crypto";
import {bindings, db, migrate, request} from "./db";

const now = Math.floor(Date.now() / 1000);
const token = "health-blue-worker-key";
const config = {...bindings, WORKER_BLUE_KEY_HASH: ""};

beforeAll(async () => {
  await migrate();
  config.WORKER_BLUE_KEY_HASH = await hmac(bindings.KEY_PEPPER!, token);
  const pools = {pool_blue: {owner: "blue", enabled: false, type: "ascend-a3", nodes: [{
    host_id: "198.51.100.40:22", allocation_groups: [{devices: [0,1], verified: true}, {devices: [2,3], verified: true}],
  }]}};
  await db.prepare("INSERT INTO pools_configs VALUES(1,'hash',?,'{}','test',?,NULL)").bind(JSON.stringify(pools), now).run();
});

beforeEach(async () => {
  await db.batch(["device_group_states", "device_occupancy_intervals", "device_occupancy_readings"]
    .map(table => db.prepare(`DELETE FROM ${table}`)));
});

test("occupancy history follows sample time across retries, missing evidence and snapshots", async () => {
  const base = {w: "blue", i: "0123abcd"};
  expect((await worker("heartbeat", {...base, rev: 1, ok: 1, act: 0, q: 0})).status).toBe(200);
  const group = {pool: "pool_blue", h: "198.51.100.40:22", d: [3,2]};
  const start = now - 2400;
  const readings = [
    {st: "busy", ts: start},
    {st: "busy", ts: start + 120},
    {st: "busy", ts: start + 120}, // same reading in a different event
    {st: "idle", ts: start + 60}, // delayed stale sample
    {st: "probe_error", ts: start + 240},
    {st: "busy", ts: start + 360},
    {sk: 1, ts: start + 480},
    {st: "busy", ts: start + 600},
    {st: "idle", ts: start + 720},
    {st: "busy", ts: start + 840},
    {st: "idle", ts: start + 1320}, // gap leaves the release unknown
    {st: "busy", ts: start + 1440},
  ].map((reading, index) => ({...group, ...reading, s: index + 1, t: "IP"}));
  // A recovery snapshot can be newer than unacknowledged observations.
  const snapshot = {...base, sid: "0011223344", p: 1, n: 1, seq: 0, kind: "device",
    items: [{...group, st: "busy", ts: now - 30}]};
  expect(await (await worker("snapshots", snapshot)).json()).toMatchObject({complete: true});
  for (let index = 0; index < readings.length; index += 6) {
    const packet = {...base, ev: readings.slice(index, index + 6)};
    expect(await (await worker("events", packet)).json()).toMatchObject({need_snapshot: false});
    expect(await (await worker("events", packet)).json()).toMatchObject({need_snapshot: false});
  }
  const timeline = await (await request("/public/timeline?history_hours=72")).json<Timeline>();
  expect(timeline.workers.find(row => row.worker_id === "blue")!.pools[0].nodes[0].device_groups[1].occupancy_blocks)
    .toEqual([
      {started_at: start, last_busy_at: start + 120, ended_at: null, close_reason: "unobserved"},
      {started_at: start + 360, last_busy_at: start + 360, ended_at: null, close_reason: "unobserved"},
      {started_at: start + 600, last_busy_at: start + 600, ended_at: start + 720, close_reason: "idle"},
      {started_at: start + 840, last_busy_at: start + 840, ended_at: null, close_reason: "unobserved"},
      {started_at: start + 1440, last_busy_at: start + 1440, ended_at: null, close_reason: null},
    ]);
  expect(await db.prepare("SELECT probe_at,occupancy_state FROM device_group_states WHERE devices_json='[2,3]'").first())
    .toMatchObject({probe_at: now - 30, occupancy_state: "busy"});
});

function worker(path: string, data: unknown) {
  return request(`/worker/${path}`, {data, headers: {Authorization: `Bearer ${token}`}}, config);
}

test("lease skips and recovery snapshots preserve the last occupancy sample", async () => {
  const base = {w: "blue", i: "abcdabcd"};
  const group = {pool: "pool_blue", h: "198.51.100.40:22", d: [0,1]};
  await worker("heartbeat", {...base, rev: 1, ok: 1, act: 0, q: 0});
  expect((await worker("events", {...base, ev: [
    {...group, s: 1, t: "IP", st: "idle", ts: now - 40},
    {...group, s: 2, t: "IP", sk: 1, ts: now - 20},
  ]})).status).toBe(200);
  expect(await (await worker("snapshots", {...base, sid: "1111222233", p: 1, n: 1, seq: 2,
    kind: "device", items: [{...group, sk: 1, ts: now - 10}]})).json()).toMatchObject({complete: true});
  expect(await db.prepare("SELECT occupancy_state,probe_at FROM device_group_states").first())
    .toMatchObject({occupancy_state: "idle", probe_at: now - 40});
  const timeline = await (await request("/public/timeline?fixture=skip")).json<Timeline>();
  expect(timeline.workers[0].pools[0].nodes[0].device_groups[0].state).toBe("CONFIRMED_IDLE");
});

test("heartbeats and config snapshots cannot advance a group's last successful visibility time", async () => {
  const base = {w: "blue", i: "aaaabbbb", rev: 1, ok: 1, act: 0, q: 0};
  const group = {pool: "pool_blue", h: "198.51.100.40:22"};
  expect((await worker("heartbeat", base)).status).toBe(200);
  const online = [now - 100, now - 80];
  expect((await worker("events", {...base, ev: online.map((ts,index) => ({
    ...group, s: index + 1, t: "IP", d: index === 0 ? [0,1] : [2,3], st: "idle", ts, pr: 1, v: 0, lo: ts,
  }))})).status).toBe(200);

  async function verify(tag: string) {
    const response = await (await request(`/public/timeline?fixture=${tag}`)).json<Timeline>();
    expect(response.workers[0].pools[0].nodes[0].device_groups.map(group => group.health.last_online_at)).toEqual(online);
    const cluster = await (await request(`/public/cluster?fixture=${tag}`)).json<{
      config: {pool_blue: {nodes: Array<{allocation_groups: Array<{last_online_at: number}>}>}};
    }>();
    expect(cluster.config.pool_blue.nodes[0].allocation_groups.map(group => group.last_online_at))
      .toEqual(online);
  }
  await verify("legacy-heartbeat");
  await worker("heartbeat", {...base, probe_ts: now - 20});
  await verify("worker-probe");
  await worker("heartbeat", {...base, rev: 2, probe_ts: null});
  expect(await db.prepare("SELECT last_visibility_probe_at FROM workers WHERE worker_id='blue'").first("last_visibility_probe_at"))
    .toBe(now - 20);
  expect(await (await worker("snapshots", {...base, sid: "1111222244", p: 1, n: 1, seq: 2, kind: "device",
    items: [{...group, d: [0,1], st: "probe_error", ts: now - 10, pr: 2, v: 1, mc: 1, lo: online[0]},
      {...group, d: [2,3]}]})).json()).toMatchObject({complete: true});
  await verify("failed-probe-and-config");
});

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
  expect(await db.prepare("SELECT visibility_state,consecutive_failures,last_online_at,last_probe_round FROM device_group_states WHERE devices_json='[0,1]'").first())
    .toMatchObject({visibility_state: "offline", consecutive_failures: 5, last_online_at: now, last_probe_round: 6});
  const timeline = await (await request("/public/timeline")).json<{workers: Array<{worker_id: WorkerId; pools: Array<{nodes: Array<{device_groups: Array<{health: unknown; state: string}>}>}>}>}>();
  const group = timeline.workers.find(worker => worker.worker_id === "blue")!.pools[0].nodes[0].device_groups[0];
  expect(group).toMatchObject({state: "CONFIRMED_IDLE", health: {status: "OFFLINE", missed_count: 5, last_online_at: now}});
});
