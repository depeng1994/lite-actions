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
  const blocked = await (await request("/admin/cluster/preview", {cookie, data: payload})).json<ClusterPreview>();
  expect(blocked.blocking_reasons).toContain("RESOURCE_NOT_DRAINED");
  const drain = {base_revision: rev, operation: {op: "DRAIN", scope: "node", pool: "alpha", host_id: host}};
  const drainPreview = await (await request("/admin/cluster/preview", {cookie, data: drain})).json<ClusterPreview>();
  const drained = await (await request("/admin/cluster/publish", {cookie,
    data: {...drain, preview_hash: drainPreview.preview_hash}})).json<ClusterSync>();
  await db.prepare(`INSERT INTO workers(worker_id,instance_id,applied_rev,code_sha,health_ok,active_count,queued_count,last_heartbeat_at,last_egress_used,updated_at)
    VALUES('blue','drain001',?, '',1,0,0,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET applied_rev=excluded.applied_rev`)
    .bind(drained.revision, 1, 0, 1).run();
  const pre = await (await request("/admin/cluster/preview", {cookie, data: {...payload, base_revision: drained.revision}})).json<ClusterPreview>();
  expect(pre.blocking_reasons ?? []).not.toContain("RESOURCE_NOT_DRAINED");
  const deletion = await (await request("/admin/cluster/publish", {cookie,
    data: {...payload, base_revision: drained.revision, preview_hash: pre.preview_hash}})).json<ClusterSync>();
  const add = {base_revision: deletion.revision, operation: {op: "ADD_NODE", pool: "beta", value: node}};
  expect(await (await request("/admin/cluster/preview", {cookie, data: add})).json()).toMatchObject({code: "SOURCE_WORKER_NOT_DRAINED"});
});

test("pool drain uses enabled=false and resume clears the drain marker", async () => {
  const cookie = sessionCookie(await signIn());
  const baseRevision = await db.prepare("SELECT MAX(revision) AS r FROM pools_configs").first<number>("r");
  const drain = {base_revision: baseRevision, operation: {op: "DRAIN", scope: "pool", pool: "beta"}};
  const preview = await (await request("/admin/cluster/preview", {cookie, data: drain})).json<ClusterPreview>();
  const published = await (await request("/admin/cluster/publish", {cookie,
    data: {...drain, preview_hash: preview.preview_hash}})).json<ClusterSync>();
  const stored = await db.prepare("SELECT config_json FROM pools_configs WHERE revision=?")
    .bind(published.revision).first<string>("config_json");
  expect(JSON.parse(stored!).beta).toMatchObject({enabled: false});
  expect(await db.prepare("SELECT count(*) AS n FROM cluster_drain_targets WHERE pool_name='beta'").first("n")).toBe(1);
  const publicView = await (await request("/public/cluster?drain_check=1"))
    .json<{config: Record<string, {draining: boolean; enabled: boolean}>}>();
  expect(publicView.config.beta).toMatchObject({draining: true, enabled: false});

  const resume = {base_revision: published.revision, operation: {op: "RESUME", scope: "pool", pool: "beta"}};
  const resumePreview = await (await request("/admin/cluster/preview", {cookie, data: resume})).json<ClusterPreview>();
  const resumed = await (await request("/admin/cluster/publish", {cookie,
    data: {...resume, preview_hash: resumePreview.preview_hash}})).json<ClusterSync>();
  const resumedConfig = await db.prepare("SELECT config_json FROM pools_configs WHERE revision=?")
    .bind(resumed.revision).first<string>("config_json");
  expect(JSON.parse(resumedConfig!).beta).toMatchObject({enabled: true});
  expect(await db.prepare("SELECT count(*) AS n FROM cluster_drain_targets WHERE pool_name='beta'").first("n")).toBe(0);
});
