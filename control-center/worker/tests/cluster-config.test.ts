import {beforeAll, expect, test} from "vitest";
import type {ClusterPreview, ClusterSync, DeviceHealth} from "../src/types/api";
import {hmac, sha256} from "../src/utils/crypto";
import {bindings, db, migrate, request, sessionCookie, signIn} from "./db";

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

test("quarantine blocks deletion in preview/publish and drain computes cancel keys with exact scope", async () => {
  const cookie = sessionCookie(await signIn());
  const rev = await db.prepare("SELECT MAX(revision) AS r FROM pools_configs").first<number>("r");
  const payload = {base_revision: rev, operation: {op: "DELETE_NODE", pool: "alpha", host_id: host}};
  await db.prepare(`INSERT INTO device_group_states(worker_id,pool_name,host_id,devices_json,quarantined,updated_at)
    VALUES('blue','alpha',?,'[0,1]',1,1)`).bind(host).run();
  // Preview must return 200 with blocking_reasons; publish rejects with 409.
  const quarantinedPreviewResp = await request("/admin/cluster/preview", {cookie, data: payload});
  expect(quarantinedPreviewResp.status).toBe(200);
  const quarantinedPreview = await quarantinedPreviewResp.json<ClusterPreview>();
  expect(quarantinedPreview.blocking_reasons).toContain("NODE_QUARANTINED");
  expect((await request("/admin/cluster/publish", {cookie, data: {...payload, preview_hash: quarantinedPreview.preview_hash}})).status).toBe(409);

  await db.prepare("DELETE FROM device_group_states").run();
  // Add a queued task uniquely dependent on group 0 ([0,1]) and another that can fall back to group 1 ([2,3]).
  await db.batch([
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,updated_at)
      VALUES('drain_only','drainonly001','key:drain_only','blue','github','org/model','model','r1',1,
        'case_000_d','t','t','npu_smoke','alpha',1,2,'QUEUED',1,1)`),
    db.prepare(`INSERT INTO tasks_active(task_uid,short_tid,task_key,worker_id,provider,repo,logical_model,run_id,
      attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,phase,timing_reliable,updated_at)
      VALUES('drain_alt','drainalt0001','key:drain_alt','blue','github','org/model','model','r2',1,
        'case_000_a','t','t','npu_smoke','alpha',1,2,'QUEUED',1,1)`),
    db.prepare(`INSERT INTO queue_orders VALUES('blue','alpha','npu_smoke','drain_only',0,'{"options":[[0]]}',1,1)`),
    db.prepare(`INSERT INTO queue_orders VALUES('blue','alpha','npu_smoke','drain_alt',1,'{"options":[[0],[1]]}',1,1)`),
  ]);
  const groupDrain = {base_revision: rev, operation: {op: "DRAIN", scope: "device_group", pool: "alpha", host_id: host, devices: [0, 1]}};
  const groupDrainPreview = await (await request("/admin/cluster/preview", {cookie, data: groupDrain})).json<ClusterPreview>();
  expect(groupDrainPreview.drain_cancel_tasks).toEqual(["drain_only"]);
  const groupDrained = await (await request("/admin/cluster/publish", {cookie,
    data: {...groupDrain, preview_hash: groupDrainPreview.preview_hash}})).json<ClusterSync>();
  const syncCmd = await db.prepare("SELECT payload_json FROM commands WHERE cmd_id=?").bind(groupDrained.command_ids[0]).first<string>("payload_json");
  expect(JSON.parse(syncCmd!).drain_cancel_keys).toEqual(["key:drain_only"]);
  // Device-group drain must NOT mark the entire pool or node as draining.
  const scopedCluster = await (await request("/admin/cluster", {cookie})).json<{
    config: {alpha: {draining: boolean; nodes: Array<{draining: boolean; allocation_groups: Array<{devices: number[]; draining: boolean}>}>}};
  }>();
  expect(scopedCluster.config.alpha.draining).toBe(false);
  expect(scopedCluster.config.alpha.nodes[0].draining).toBe(false);
  expect(scopedCluster.config.alpha.nodes[0].allocation_groups.map(g => g.draining)).toEqual([true, false]);

  // Clear queued tasks and resume group drain before testing node drain -> delete -> cross-zone guard.
  await db.batch([
    db.prepare("DELETE FROM queue_orders"),
    db.prepare("DELETE FROM tasks_active"),
  ]);
  const blocked = await (await request("/admin/cluster/preview", {cookie, data: {...payload, base_revision: groupDrained.revision}})).json<ClusterPreview>();
  expect(blocked.blocking_reasons).toContain("RESOURCE_NOT_DRAINED");
  const drain = {base_revision: groupDrained.revision, operation: {op: "DRAIN", scope: "node", pool: "alpha", host_id: host}};
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

test("PARTIAL_APPLIED receipts update applied_rev, expose admin error_msg safely, and cluster fuses device health", async () => {
  const cookie = sessionCookie(await signIn());
  const now = Math.floor(Date.now() / 1000);
  const blueToken = "worker-blue-partial-token";
  const workerEnv = {...bindings, WORKER_BLUE_KEY_HASH: await hmac("test-pepper", blueToken)};
  const rev = await db.prepare("SELECT MAX(revision) AS r FROM pools_configs").first<number>("r");
  const add = {base_revision: rev, operation: {op: "ADD_NODE", pool: "alpha", value: {
    host_id: "198.51.100.9:22", ssh: "198.51.100.9:22", node_ip: "192.0.2.9", network_interface: "eth0",
    machine_fingerprint: "b".repeat(64), device_ids: [0, 1], allocation_groups: [{devices: [0, 1], verified: true}],
  }}};
  const preview = await (await request("/admin/cluster/preview", {cookie, data: add})).json<ClusterPreview>();
  const published = await (await request("/admin/cluster/publish", {cookie, data: {...add, preview_hash: preview.preview_hash}})).json<ClusterSync>();
  const cmdId = published.command_ids[0];
  const headers = {Authorization: `Bearer ${blueToken}`};
  expect((await request("/worker/command-receipts", {headers, data: {cmd_id: cmdId, status: "ACKED", ts: now}}, workerEnv)).status).toBe(200);
  const partialResp = await request("/worker/command-receipts", {
    headers,
    data: {cmd_id: cmdId, status: "PARTIAL_APPLIED", rev: published.revision, err: "198.51.100.9:22: Permission denied", ts: now},
  }, workerEnv);
  expect(partialResp.status).toBe(200);

  await db.prepare(`INSERT INTO device_group_states(worker_id,pool_name,host_id,devices_json,occupancy_state,probe_at,
    quarantined,visibility_state,consecutive_failures,last_online_at,last_probe_round,updated_at)
    VALUES('blue','alpha','198.51.100.9:22','[0,1]','idle',?,0,'offline',5,?,6,?)
    ON CONFLICT(worker_id,pool_name,host_id,devices_json) DO UPDATE SET
      visibility_state='offline',consecutive_failures=5,last_online_at=excluded.last_online_at,updated_at=excluded.updated_at`)
    .bind(now, now - 600, now).run();

  const adminView = await (await request("/admin/cluster", {cookie})).json<{
    workers: Array<{worker_id: string; applied_rev: number; status: string; error_msg: string | null}>;
    config: {alpha: {health: DeviceHealth; nodes: Array<{host_id: string; health: DeviceHealth; allocation_groups: Array<{health: DeviceHealth; occupancy_state: string}>}>}};
  }>();
  const blueWorker = adminView.workers.find(w => w.worker_id === "blue")!;
  expect(blueWorker).toMatchObject({applied_rev: published.revision, status: "PARTIAL_APPLIED", error_msg: "198.51.100.9:22: Permission denied"});
  expect(adminView.config.alpha.health.status).toBe("OFFLINE");
  expect(adminView.config.alpha.nodes[0].health).toEqual({status: "OFFLINE", missed_count: 5, last_online_at: now - 600});
  expect(adminView.config.alpha.nodes[0].allocation_groups[0].occupancy_state).toBe("CONFIRMED_IDLE");

  const publicText = await (await request("/public/cluster?partial_check=1")).text();
  expect(publicText).not.toContain("198.51.100.9");
  const publicView = JSON.parse(publicText) as {
    workers: Array<{worker_id: string; status: string; error_msg?: string}>;
    config: {alpha: {health: DeviceHealth}};
  };
  expect(publicView.workers.find(w => w.worker_id === "blue")).toMatchObject({status: "PARTIAL_APPLIED"});
  expect(publicView.workers.find(w => w.worker_id === "blue")?.error_msg).toBeUndefined();
  expect(publicView.config.alpha.health.status).toBe("OFFLINE");
});

test("SET_OCCUPANCY_POLICY updates node policy without dropping multinode bundles", async () => {
  const cookie = sessionCookie(await signIn());
  const host2 = "198.51.100.10:22";
  const rev0 = await db.prepare("SELECT MAX(revision) AS r FROM pools_configs").first<number>("r");
  const addPool = {base_revision: rev0, operation: {op: "ADD_POOL", pool: "preempt_pool", value: {
    owner: "blue", type: "ascend-a3", enabled: true, force_reclaim: true, process_indexing: "card-die",
    nodes: [
      {host_id: host, ssh: host, node_ip: "192.0.2.1", network_interface: "eth0",
        machine_fingerprint: "c".repeat(64), device_ids: [0, 1], occupancy_policy: "wait",
        allocation_groups: [{devices: [0, 1], verified: true}]},
      {host_id: host2, ssh: host2, node_ip: "192.0.2.10", network_interface: "eth0",
        machine_fingerprint: "d".repeat(64), device_ids: [0, 1], occupancy_policy: "wait",
        allocation_groups: [{devices: [0, 1], verified: true}]},
    ],
    verified_multinode_allocations: [[{host_id: host, devices: [0, 1]}, {host_id: host2, devices: [0, 1]}]],
  }}};
  const p0 = await (await request("/admin/cluster/preview", {cookie, data: addPool})).json<ClusterPreview>();
  const r1 = await (await request("/admin/cluster/publish", {cookie, data: {...addPool, preview_hash: p0.preview_hash}})).json<ClusterSync>();

  const op = {base_revision: r1.revision, operation: {op: "SET_OCCUPANCY_POLICY", pool: "preempt_pool", host_id: host, occupancy_policy: "preempt"}};
  const preview = await (await request("/admin/cluster/preview", {cookie, data: op})).json<ClusterPreview>();
  expect(preview.blocking_reasons ?? []).toEqual([]);
  const synced = await (await request("/admin/cluster/publish", {cookie, data: {...op, preview_hash: preview.preview_hash}})).json<ClusterSync>();
  expect(synced.command_ids).toHaveLength(1);
  const stored = JSON.parse((await db.prepare("SELECT config_json FROM pools_configs WHERE revision=?").bind(synced.revision).first<string>("config_json"))!);
  expect(stored.preempt_pool.nodes.find((n: {host_id: string}) => n.host_id === host).occupancy_policy).toBe("preempt");
  expect(stored.preempt_pool.nodes.find((n: {host_id: string}) => n.host_id === host2).occupancy_policy).toBe("wait");
  expect(stored.preempt_pool.verified_multinode_allocations).toHaveLength(1);
});
