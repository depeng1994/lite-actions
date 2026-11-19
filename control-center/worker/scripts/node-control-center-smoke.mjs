import assert from "node:assert/strict";
import {execFileSync} from "node:child_process";
import {mkdtempSync, mkdirSync, rmSync, writeFileSync} from "node:fs";
import {tmpdir} from "node:os";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {buildSync} from "esbuild";
import {startNodeServer} from "../src/adapters/nodeServer.ts";

const root = mkdtempSync(path.join(tmpdir(), "lite-actions-node-control-center-"));
const dbPath = path.join(root, "control.sqlite");
const staticDir = path.join(root, "client");
mkdirSync(staticDir);
writeFileSync(path.join(staticDir, "index.html"), "<!doctype html><title>control center</title>");
execFileSync(process.execPath, [fileURLToPath(new URL("./seed.mjs", import.meta.url)), "--sqlite", dbPath], {stdio: "ignore"});
const bundlePath = path.join(root, "app.mjs");
buildSync({entryPoints: [fileURLToPath(new URL("../src/index.ts", import.meta.url))], bundle: true, format: "esm", platform: "node", outfile: bundlePath, logLevel: "silent"});
const {default: app} = await import(bundlePath);

const server = startNodeServer({
  dbPath,
  staticDir,
  port: 8790,
  env: {SUPER_SECRET: "test-super-secret", KEY_PEPPER: "test-pepper", DEV_INSECURE_COOKIE: "1"},
  handler: (request, env) => app.fetch(request, env),
});
try {
  await server.ready;
  const base = "http://127.0.0.1:8790";
  const page = await fetch(`${base}/models`, {headers: {accept: "text/html"}});
  assert.equal(page.status, 200);
  assert.match(await page.text(), /control center/);
  const overview = await fetch(`${base}/api/v1/public/overview`);
  const overviewBody = await overview.text();
  assert.equal(overview.status, 200, overviewBody);
  assert.equal(JSON.parse(overviewBody).page_refresh_sec, 30);
  const login = await fetch(`${base}/api/v1/auth/login`, {
    method: "POST", headers: {"content-type": "application/json"},
    body: JSON.stringify({username: "super", secret: "test-super-secret"}),
  });
  assert.equal(login.status, 200);
  const cookie = login.headers.get("set-cookie")?.split(";")[0];
  assert.ok(cookie);
  const cluster = await fetch(`${base}/api/v1/admin/cluster`, {headers: {cookie}});
  assert.equal(cluster.status, 200);
  assert.ok(Array.isArray((await cluster.json()).history));
  const workflows = await fetch(`${base}/api/v1/public/workflows?history_limit=50`);
  assert.equal(workflows.status, 200);
  assert.equal((await workflows.json()).active.length, 0);
} finally {
  await server.close();
  rmSync(root, {recursive: true, force: true});
}
console.log("node control-center smoke passed");
