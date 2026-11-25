import {expect, test} from "vitest";
import {mkdtempSync, readFileSync, readdirSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {createNodeSqliteStore} from "../src/adapters/nodeSqlite";
import {handleRequest, runMaintenance} from "../src/app";
import {startNodeServer} from "../src/adapters/nodeServer";
import type {Env} from "../src/router";

function migrate(store: ReturnType<typeof createNodeSqliteStore>) {
  for (const file of readdirSync(new URL("../migrations/", import.meta.url)).filter(name => name.endsWith(".sql")).sort()) {
    store.database.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), "utf8"));
  }
}

test("Node runtime serves the API through the same request handler", async () => {
  const root = mkdtempSync(join(tmpdir(), "lite-actions-node-runtime-"));
  const dbPath = join(root, "control-center.sqlite");
  const db = createNodeSqliteStore(dbPath);
  migrate(db);
  const env: Env = {DB: db, SUPER_SECRET: "test-super-secret", KEY_PEPPER: "test-pepper"};
  const response = await handleRequest(new Request("https://example.test/api/v1/public/overview"), env, {clientIp: "192.0.2.10"});
  expect(response.status).toBe(200);
  expect((await response.json()) as object).toHaveProperty("workers");
  db.close();
});

test("Node server binds on loopback and closes cleanly", async () => {
  const root = mkdtempSync(join(tmpdir(), "lite-actions-node-server-"));
  const dbPath = join(root, "control-center.sqlite");
  const server = startNodeServer({
    dbPath,
    host: "127.0.0.1",
    port: 0,
    enableTimers: false,
    handler: async (request, env, options) => handleRequest(request, env as unknown as Env, options),
  });
  await server.ready;
  expect(server.server.address()).toBeTruthy();
  await server.close();
});

test("Node server startup maintenance self-heals pre-existing stopped test worker orphans", async () => {
  const root = mkdtempSync(join(tmpdir(), "lite-actions-node-heal-"));
  const dbPath = join(root, "control-center.sqlite");
  const seed = createNodeSqliteStore(dbPath);
  migrate(seed);
  const now = Math.floor(Date.now() / 1000);
  const testWorker = "blue-test-id45542029869a1";
  await seed.batch([
    seed.prepare(`INSERT INTO test_workers(
      worker_id,zone,repo,pr_number,pr_head_sha,run_id,attempt,status,ttl_seconds,
      expires_at,registered_at,deregistered_at,instance_id,last_seq,last_snapshot_id,updated_at
    ) VALUES (?,'blue','depeng1994/lite-actions',56,?,'45542029869',1,'STOPPED',14400,?,?,?,?,2,NULL,?)`)
      .bind(testWorker, "a".repeat(40), now + 7200, now - 3000, now - 1000, "bb112233", now - 1000),
    seed.prepare(`INSERT INTO tasks_active(
      task_uid,short_tid,task_key,worker_id,reporter_id,is_test,provider,repo,logical_model,
      run_id,attempt,case_id,test_id,label,kind,target_pool,req_nodes,req_devices,
      phase,allocation_json,queued_at,running_at,timing_reliable,updated_at
    ) VALUES ('t_orphan_4554','orphan455401','k_orphan_4554','blue',?,1,'github','depeng1994/lite-actions','lite-actions',
      '45542029869',1,'case_005_dispatch_smoke_16p','dispatch_smoke_16p','Dispatch 16p','npu_smoke','a3_shared_blue',2,8,
      'RUNNING','[]',?,?,1,?)`).bind(testWorker, now - 2500, now - 2132, now - 2132),
    seed.prepare(`INSERT INTO workflow_runs(
      provider,repo,run_id,attempt,is_test,reporter_ids_json,logical_model,
      is_active,has_in_flight,has_waiting_resource_case,active_case_count,total_case_count,
      result_state,pools_json,max_cards,card_seconds_finished,has_unmeasured_allocated,updated_at
    ) VALUES ('github','depeng1994/lite-actions','45542029869',1,1,?,'lite-actions',
      1,1,0,1,1,'QUEUED','["a3_shared_blue"]',16,0,0,?)`).bind(JSON.stringify([testWorker]), now - 2132),
  ]);
  seed.close();

  const env = {SUPER_SECRET: "test-super-secret", KEY_PEPPER: "test-pepper"};
  const server = startNodeServer({
    dbPath,
    host: "127.0.0.1",
    port: 0,
    env,
    enableTimers: true,
    handler: async (request, requestEnv, options) => handleRequest(request, requestEnv as unknown as Env, options),
    maintenance: async (kind, maintEnv) => runMaintenance(kind, maintEnv as unknown as Env),
  });
  await server.ready;
  await server.flushBackground();

  expect(await server.store.prepare("SELECT count(*) AS n FROM tasks_active WHERE task_uid='t_orphan_4554'").first("n")).toBe(0);
  expect(await server.store.prepare("SELECT status,exit_code FROM task_index WHERE task_uid='t_orphan_4554'").first())
    .toEqual({status: "CANCELLED", exit_code: 130});
  expect(await server.store.prepare("SELECT is_active,result_state,active_case_count FROM workflow_runs WHERE run_id='45542029869' AND is_test=1").first())
    .toEqual({is_active: 0, result_state: "FAIL", active_case_count: 0});
  await server.close();
});
