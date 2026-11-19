import {beforeAll, expect, test} from "vitest";
import type {ClusterPreview, ClusterSync} from "../src/types/api";
import {sha256} from "../src/utils/crypto";
import {db, migrate, request, sessionCookie, signIn} from "./db";

const host = "198.51.100.1:22";
const node = {host_id: host, ssh: host, node_ip: "192.0.2.1", network_interface: "eth0", machine_fingerprint: "a".repeat(64),
  device_ids: [0,1,2,3], allocation_groups: [{devices: [0,1], verified: true}, {devices: [2,3], verified: true}]};
const base = {alpha: {owner: "blue", type: "ascend-a3", enabled: true, process_indexing: "card-die", nodes: [node]},
  beta: {owner: "yellow", type: "ascend-a3", enabled: true, process_indexing: "card-die", nodes: []}};
beforeAll(async () => {
  await migrate();
  const config = JSON.stringify(base);
  await db.prepare("INSERT INTO pools_configs VALUES(1,?,?, '{}','bootstrap',1,NULL)").bind(await sha256(config), config).run();
});

test("preview, publish, revision conflict, restore and rollback keep immutable revisions", async () => {
  const cookie = sessionCookie(await signIn());
  const op = {op: "PAUSE", scope: "device_group", pool: "alpha", host_id: host, devices: [1,0]};
  const data = {base_revision: 1, operation: op};
  const preview = await (await request("/admin/cluster/preview", {cookie, data})).json<ClusterPreview>();
  expect(preview.affected_groups).toEqual([{worker_id: "blue", pool_name: "alpha", host_id: host, devices: [0,1]}]);
  expect((await request("/admin/cluster/publish", {cookie, data: {...data, preview_hash: "0".repeat(64)}})).status).toBe(409);
  const published = await (await request("/admin/cluster/publish", {cookie, data: {...data, preview_hash: preview.preview_hash}})).json<ClusterSync>();
  expect(published).toMatchObject({revision: 2, command_ids: [expect.any(String)]});
  expect((await request("/admin/cluster/preview", {cookie, data})).status).toBe(409);
  const resume = {base_revision: 2, operation: {...op, op: "RESUME"}};
  const confirmation = await (await request("/admin/cluster/preview", {cookie, data: resume})).json<ClusterPreview>();
  const restored = await (await request("/admin/cluster/publish", {cookie, data: {...resume, preview_hash: confirmation.preview_hash}})).json<ClusterSync>();
  expect(restored.revision).toBe(3);
  const stored = await db.prepare("SELECT config_json FROM pools_configs WHERE revision=3").first<string>("config_json");
  expect(JSON.parse(stored!).alpha.nodes[0].allocation_groups[0]).not.toHaveProperty("paused");
  const rollback = await (await request("/admin/cluster/rollback", {cookie, data: {base_revision: 3, target_revision: 2}})).json<ClusterSync>();
  expect(rollback.revision).toBe(4);
  expect(rollback.sha256).toBe(published.sha256);
  const publicCluster = await (await request("/public/cluster")).text();
  expect(publicCluster).not.toMatch(/198\.51\.100|machine_fingerprint|"ssh"/);
});

test("quarantine blocks deletion and unacknowledged removal blocks cross-zone move", async () => {
  const cookie = sessionCookie(await signIn());
  const rev = await db.prepare("SELECT MAX(revision) AS r FROM pools_configs").first<number>("r");
  const payload = {base_revision: rev, operation: {op: "DELETE_NODE", pool: "alpha", host_id: host}};
  await db.prepare(`INSERT INTO device_group_states(worker_id,pool_name,host_id,devices_json,quarantined,updated_at)
    VALUES('blue','alpha',?,'[0,1]',1,1)`).bind(host).run();
  expect(await (await request("/admin/cluster/preview", {cookie, data: payload})).json()).toMatchObject({code: "NODE_QUARANTINED"});
  await db.prepare("DELETE FROM device_group_states").run();
  const pre = await (await request("/admin/cluster/preview", {cookie, data: payload})).json<ClusterPreview>();
  const deletion = await (await request("/admin/cluster/publish", {cookie, data: {...payload, preview_hash: pre.preview_hash}})).json<ClusterSync>();
  const add = {base_revision: deletion.revision, operation: {op: "ADD_NODE", pool: "beta", value: node}};
  expect(await (await request("/admin/cluster/preview", {cookie, data: add})).json()).toMatchObject({code: "SOURCE_WORKER_NOT_DRAINED"});
});
