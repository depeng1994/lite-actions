import {expect} from "vitest";
import {capacity, retention} from "../../src/services/retention";
import type {Env} from "../../src/router";
import type {SqlStore} from "../../src/storage/sqlStore";
import {hmac, sha256} from "../../src/utils/crypto";

export interface ParityHarness {
  name: string;
  store: SqlStore;
  env: Env;
  now: number;
  fetch: (path: string, init?: {method?: string; headers?: Record<string, string>; body?: string; clientIp?: string}) => Promise<Response>;
  flushBackground: () => Promise<void>;
}

export async function runPortabilityParitySuite(harness: ParityHarness) {
  const {store, env, now} = harness;
  const rawHost = "198.51.100.42:22";
  const poolsConfig = {
    parity_pool: {
      owner: "blue",
      type: "ascend-a3",
      enabled: true,
      process_indexing: "card-die",
      nodes: [{
        host_id: rawHost,
        ssh: rawHost,
        node_ip: "192.0.2.42",
        network_interface: "eth0",
        machine_fingerprint: "a".repeat(64),
        device_ids: [0, 1],
        allocation_groups: [{devices: [0, 1], verified: true}],
      }],
    },
  };
  const configJson = JSON.stringify(poolsConfig);
  const configSha = await sha256(configJson);
  await store.prepare(`INSERT INTO pools_configs(revision,sha256,config_json,diff_summary_json,created_by,created_at)
    VALUES (1,?,?,'{}','parity',?) ON CONFLICT(revision) DO NOTHING`).bind(configSha, configJson, now - 300).run();

  // 1. Static assets, SPA fallback, and strict JSON 404 on /api/*
  const assetResp = await harness.fetch("/assets/app.js");
  expect(assetResp.status, `${harness.name}: static asset`).toBe(200);
  expect(assetResp.headers.get("content-type")).toContain("javascript");

  const spaResp = await harness.fetch("/models", {headers: {accept: "text/html"}});
  expect(spaResp.status, `${harness.name}: SPA fallback`).toBe(200);
  expect(await spaResp.text()).toContain("control-center-parity-shell");

  const missingApi = await harness.fetch("/api/v1/does-not-exist", {headers: {accept: "text/html"}});
  expect(missingApi.status, `${harness.name}: API 404 JSON`).toBe(404);
  expect(missingApi.headers.get("content-type")).toContain("application/json");
  expect(await missingApi.json()).toMatchObject({code: "NOT_FOUND"});

  const postUnknown = await harness.fetch("/unknown-route", {method: "POST", headers: {accept: "text/html"}});
  expect(postUnknown.status, `${harness.name}: POST unknown not SPA`).toBeGreaterThanOrEqual(404);

  // 2. Public read masking & Cache-Control
  const overviewResp = await harness.fetch("/api/v1/public/overview");
  expect(overviewResp.status).toBe(200);
  expect(overviewResp.headers.get("cache-control")).toBe("public, max-age=10");
  expect(await overviewResp.json()).toMatchObject({page_refresh_sec: 30});

  const pubTimelineResp = await harness.fetch("/api/v1/public/timeline");
  expect(pubTimelineResp.status).toBe(200);
  expect(pubTimelineResp.headers.get("cache-control")).toBe("public, max-age=10");
  const pubTimelineText = await pubTimelineResp.text();
  expect(pubTimelineText).not.toContain("198.51.100.42");

  // 3. Super login without timingSafeEqual & independent per-IP 5-failure lockout (429)
  for (let i = 0; i < 5; i += 1) {
    const failed = await harness.fetch("/api/v1/auth/login", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({username: "super", secret: "wrong-password"}),
      clientIp: "198.51.100.77",
    });
    expect(failed.status).toBe(401);
  }
  const locked = await harness.fetch("/api/v1/auth/login", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({username: "super", secret: "wrong-password"}),
    clientIp: "198.51.100.77",
  });
  expect(locked.status).toBe(429);

  const otherIp = await harness.fetch("/api/v1/auth/login", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({username: "super", secret: "wrong-password"}),
    clientIp: "198.51.100.88",
  });
  expect(otherIp.status).toBe(401);

  const loginOk = await harness.fetch("/api/v1/auth/login", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({username: "super", secret: env.SUPER_SECRET}),
    clientIp: "198.51.100.99",
  });
  expect(loginOk.status).toBe(200);
  const cookie = loginOk.headers.get("set-cookie")?.split(";")[0];
  expect(cookie).toContain("la_session=");

  const adminTimelineResp = await harness.fetch("/api/v1/admin/timeline", {headers: {cookie: cookie!}});
  expect(adminTimelineResp.status).toBe(200);
  expect(adminTimelineResp.headers.get("cache-control")).toBe("private, no-store");
  expect(await adminTimelineResp.text()).toContain(rawHost);

  // 4 & 5. Worker heartbeat (+ no-Cron lazy cancellation tracking), event dedup, snapshot reconciliation, and PARTIAL_APPLIED command receipt
  const blueAuth = {Authorization: "Bearer parity-blue-worker-secret", "content-type": "application/json"};
  const originalFetch = globalThis.fetch;
  let cancelStatusCalls = 0;
  try {
    await store.prepare(`INSERT INTO cancellations(cancel_id,provider,repo,run_id,attempt,status,requested_by,created_at,submitted_at,updated_at)
      VALUES ('cancel_parity_1','gitcode','cann/torchtitan-npu','gc_999',1,'SUBMITTED','super',?,?,?)`)
      .bind(now - 200, now - 200, now - 200).run();
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const target = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (target.includes("api.gitcode.com")) {
        cancelStatusCalls += 1;
        return Response.json({workflow_run_id: "gc_999", status: "CANCELED", attempt: 1});
      }
      return originalFetch(input as RequestInfo, init);
    }) as typeof fetch;

    const hb = await harness.fetch("/api/v1/worker/heartbeat", {
      method: "POST",
      headers: blueAuth,
      body: JSON.stringify({w: "blue", i: "a1b2c3d4", ts: now, rev: 1, sha: "12345678", ok: 1, act: 0, q: 0, idle_ts: now, probe_ts: now, eu: 120}),
    });
    expect(hb.status).toBe(200);
    expect(await hb.json()).toMatchObject({ok: true, latest_revision: 1});
    await harness.flushBackground();
    const updatedCancel = await store.prepare("SELECT status,platform_conclusion FROM cancellations WHERE cancel_id='cancel_parity_1'")
      .first<{status: string; platform_conclusion: string | null}>();
    expect(cancelStatusCalls).toBeGreaterThanOrEqual(1);
    expect(updatedCancel).toMatchObject({status: "COMPLETED", platform_conclusion: "canceled"});
  } finally {
    globalThis.fetch = originalFetch;
  }

  const ipEvent = {
    s: 1, t: "IP", ts: now, pool: "parity_pool", h: rawHost, d: [0, 1],
    st: "idle", pr: 1, v: 0, mc: 0, lo: now,
  };
  expect((await harness.fetch("/api/v1/worker/events", {
    method: "POST", headers: blueAuth, body: JSON.stringify({w: "blue", i: "a1b2c3d4", ev: [ipEvent]}),
  })).status).toBe(200);
  // Duplicate event must be idempotent
  expect((await harness.fetch("/api/v1/worker/events", {
    method: "POST", headers: blueAuth, body: JSON.stringify({w: "blue", i: "a1b2c3d4", ev: [ipEvent]}),
  })).status).toBe(200);

  const snapResp = await harness.fetch("/api/v1/worker/snapshots", {
    method: "POST",
    headers: blueAuth,
    body: JSON.stringify({
      w: "blue", i: "a1b2c3d4", sid: "0123456789", p: 1, n: 1, seq: 1, kind: "device",
      items: [{pool: "parity_pool", h: rawHost, d: [0, 1], st: "idle", ts: now, pr: 1, v: 0, mc: 0, lo: now}],
    }),
  });
  expect(snapResp.status).toBe(200);
  expect(await snapResp.json()).toMatchObject({ok: true, complete: true});

  await store.prepare(`INSERT INTO pools_configs(revision,sha256,config_json,diff_summary_json,created_by,created_at)
    VALUES (2,?,?,'{}','parity',?) ON CONFLICT(revision) DO NOTHING`).bind(configSha, configJson, now).run();
  await store.prepare(`INSERT INTO commands(cmd_id,worker_id,cmd_type,payload_json,status,created_by,created_at)
    VALUES ('cmd_parity_1','blue','SYNC_CONFIG',?,'PENDING','super',?)`)
    .bind(JSON.stringify({revision: 2, sha256: configSha}), now).run();
  const cmdList = await (await harness.fetch("/api/v1/worker/commands", {headers: blueAuth})).json<{commands: Array<{cmd_id: string}>}>();
  expect(cmdList.commands.some(c => c.cmd_id === "cmd_parity_1")).toBe(true);
  expect((await harness.fetch("/api/v1/worker/command-receipts", {
    method: "POST", headers: blueAuth, body: JSON.stringify({cmd_id: "cmd_parity_1", status: "ACKED", ts: now}),
  })).status).toBe(200);
  expect((await harness.fetch("/api/v1/worker/command-receipts", {
    method: "POST", headers: blueAuth,
    body: JSON.stringify({cmd_id: "cmd_parity_1", status: "PARTIAL_APPLIED", rev: 2, err: "node unreachable", ts: now}),
  })).status).toBe(200);
  const appliedRev = await store.prepare("SELECT applied_rev FROM workers WHERE worker_id='blue'").first<number>("applied_rev");
  expect(appliedRev).toBe(2);

  // 6. Cold gzip BLOB round-trip across runtimes
  const oldTime = now - 35 * 86400;
  await store.batch([
    store.prepare(`INSERT INTO task_index(task_uid,finished_at,worker_id,provider,repo,logical_model,run_id,attempt,
      case_id,test_id,label,kind,target_pool,req_nodes,req_devices,actual_allocation_json,status,timing_reliable,storage_tier)
      VALUES('t_parity_cold',?,'blue','github','depeng1994/lite-actions','lite-actions','cold-1',1,
        'case_000_cold','cold','Cold Test','npu_smoke','parity_pool',1,2,'[]','PASS',1,'hot')`).bind(oldTime),
    store.prepare("INSERT INTO task_details_hot VALUES('t_parity_cold',?,?)")
      .bind(oldTime, JSON.stringify({queued_at: oldTime - 120, running_at: oldTime - 60})),
  ]);
  await retention({
    request: new Request("https://internal.invalid/"),
    url: new URL("https://internal.invalid/"),
    env,
    params: {},
    clientIp: "system",
    now,
  });
  const tier = await store.prepare("SELECT storage_tier FROM task_index WHERE task_uid='t_parity_cold'").first<string>("storage_tier");
  expect(tier).toBe("cold");
  const coldDetailResp = await harness.fetch("/api/v1/public/tasks/t_parity_cold");
  expect(coldDetailResp.status).toBe(200);
  expect(await coldDetailResp.json()).toMatchObject({
    task_uid: "t_parity_cold",
    storage_tier: "cold",
    queued_at: oldTime - 120,
    running_at: oldTime - 60,
  });

  // 7. dbSizeBytes() and 80%->70% capacity purge
  const currentSize = await store.dbSizeBytes();
  expect(Number.isFinite(currentSize) && currentSize > 0, `${harness.name}: dbSizeBytes=${currentSize}`).toBe(true);
  let syntheticSize = 900;
  await capacity({
    request: new Request("https://internal.invalid/"),
    url: new URL("https://internal.invalid/"),
    env: {...env, DB_LIMIT_BYTES: "1000"},
    params: {},
    clientIp: "system",
    now,
  }, async () => {
    const out = syntheticSize;
    syntheticSize = 600;
    return out;
  });
  const purgedBefore = await store.prepare("SELECT meta_value FROM system_meta WHERE meta_key='capacity_purged_before_ts'")
    .first<string>("meta_value");
  expect(Number(purgedBefore)).toBeGreaterThan(0);
  expect(await store.prepare("SELECT count(*) AS n FROM task_index WHERE task_uid='t_parity_cold'").first<number>("n")).toBe(0);
}

export async function parityWorkerKeyHash(pepper: string): Promise<string> {
  return hmac(pepper, "parity-blue-worker-secret");
}
