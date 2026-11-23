import {expect, test} from "vitest";
import {mkdtempSync, readFileSync, readdirSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {createNodeSqliteStore} from "../src/adapters/nodeSqlite";
import {handleRequest} from "../src/app";
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
