import {expect, test} from "vitest";
import {aggregateHealth, projectDevice, type GroupRow, type Pool, type WorkerRow} from "../src/services/deviceState";

test("occupancy comes from a live lease or a fresh verified census", () => {
  const worker: WorkerRow = {worker_id: "blue", applied_rev: 1, health_ok: 1, last_heartbeat_at: 2020,
    last_idle_probe_at: null, active_count: 0, queued_count: 0};
  const group = {devices: [1, 0]};
  const node = {host_id: "198.51.100.3:22", allocation_groups: [group]};
  const pool: Pool = {owner: "blue", type: "ascend-a3", nodes: [node], enabled: true};
  const input = {worker, pool, node, group, leased: false};
  const idle: GroupRow = {worker_id: "blue", pool_name: "test", host_id: node.host_id,
    devices_json: "[0,1]", occupancy_state: "idle", probe_at: 100, quarantined: 1, updated_at: 100};
  expect(projectDevice(2025, input).state).toBe("UNKNOWN_BUSY");
  expect(projectDevice(1900, {...input, observation: idle}).state).toBe("CONFIRMED_IDLE");
  expect(projectDevice(1901, {...input, observation: idle}).state).toBe("UNKNOWN_BUSY");
  expect(projectDevice(2025, {...input, worker: {...worker, last_idle_probe_at: 2020}, observation: idle}).state).toBe("CONFIRMED_IDLE");
  const leased = projectDevice(2120, {...input, group: {...group, paused: true}, observation: idle, leased: true});
  expect(leased).toMatchObject({state: "LEASED", paused: true, offline: true, quarantined: true, paused_scopes: ["device_group"]});
});

test("visibility health is independent from occupancy and preserves last online time", () => {
  const worker: WorkerRow = {worker_id: "blue", applied_rev: 1, health_ok: 1, last_heartbeat_at: 2020,
    last_idle_probe_at: null, active_count: 0, queued_count: 0};
  const group = {devices: [0, 1]};
  const node = {host_id: "198.51.100.3:22", allocation_groups: [group]};
  const pool: Pool = {owner: "blue", type: "ascend-a3", nodes: [node], enabled: false};
  const observation: GroupRow = {worker_id: "blue", pool_name: "test", host_id: node.host_id,
    devices_json: "[0,1]", occupancy_state: "busy", probe_at: 100, quarantined: 0,
    visibility_state: "online", consecutive_failures: 0, last_online_at: 1980, last_probe_round: 4, updated_at: 100};
  const projected = projectDevice(2025, {worker, pool, node, group, observation, leased: false, draining: true});
  expect(projected.health).toEqual({status: "ONLINE", missed_count: 0, last_online_at: 2020});
  expect(projected.paused).toBe(true);
  expect(projected.paused_scopes).toEqual(["pool"]);
  expect(projected.draining).toBe(true);
  expect(projectDevice(2025, {worker, pool, node, group, observation: {...observation, visibility_state: "warn", consecutive_failures: 4}, leased: false}).health)
    .toEqual({status: "PROBE_WARN", missed_count: 4, last_online_at: 1980});
  expect(aggregateHealth([
    {status: "ONLINE", missed_count: 0, last_online_at: 1900},
    {status: "OFFLINE", missed_count: 5, last_online_at: 1980},
  ])).toEqual({status: "PARTIAL_OFFLINE", missed_count: 5, last_online_at: 1980});
  expect(aggregateHealth([
    {status: "OFFLINE", missed_count: 5, last_online_at: 1900},
    {status: "OFFLINE", missed_count: 5, last_online_at: 1950},
  ])).toEqual({status: "OFFLINE", missed_count: 5, last_online_at: 1950});
});

test("an online group uses the latest successful worker visibility probe", () => {
  const worker: WorkerRow = {worker_id: "yellow", applied_rev: 1, health_ok: 1, last_heartbeat_at: 2020,
    last_idle_probe_at: null, last_visibility_probe_at: 2010, active_count: 0, queued_count: 0};
  const group = {devices: [0, 1]};
  const node = {host_id: "198.51.100.25:22", allocation_groups: [group]};
  const pool: Pool = {owner: "yellow", type: "ascend-a5", nodes: [node], enabled: true};
  const observation: GroupRow = {worker_id: "yellow", pool_name: "a5_shared_yellow", host_id: node.host_id,
    devices_json: "[0,1]", occupancy_state: "busy", probe_at: 2010, quarantined: 0,
    visibility_state: "online", consecutive_failures: 0, last_online_at: 1980, last_probe_round: 4, updated_at: 2010};

  expect(projectDevice(2025, {worker, pool, node, group, observation, leased: false}).health)
    .toEqual({status: "ONLINE", missed_count: 0, last_online_at: 2010});
  expect(projectDevice(2025, {
    worker, pool, node, group,
    observation: {...observation, visibility_state: "offline", consecutive_failures: 5},
    leased: false,
  }).health).toEqual({status: "OFFLINE", missed_count: 5, last_online_at: 1980});
});

test("an online group falls back to the current heartbeat for older workers", () => {
  const worker: WorkerRow = {worker_id: "yellow", applied_rev: 1, health_ok: 1, last_heartbeat_at: 2020,
    last_idle_probe_at: null, last_visibility_probe_at: null, active_count: 0, queued_count: 0};
  const group = {devices: [0, 1]};
  const node = {host_id: "198.51.100.25:22", allocation_groups: [group]};
  const pool: Pool = {owner: "yellow", type: "ascend-a5", nodes: [node], enabled: true};
  const observation: GroupRow = {worker_id: "yellow", pool_name: "a5_shared_yellow", host_id: node.host_id,
    devices_json: "[0,1]", occupancy_state: "busy", probe_at: 1980, quarantined: 0,
    visibility_state: "online", consecutive_failures: 0, last_online_at: 1980, last_probe_round: 4, updated_at: 1980};

  expect(projectDevice(2025, {worker, pool, node, group, observation, leased: false}).health)
    .toEqual({status: "ONLINE", missed_count: 0, last_online_at: 2020});
});
