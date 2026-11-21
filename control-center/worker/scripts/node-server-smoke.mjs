import assert from "node:assert/strict";
import {mkdtempSync, mkdirSync, writeFileSync, rmSync} from "node:fs";
import {tmpdir} from "node:os";
import path from "node:path";
import {startNodeServer} from "../src/adapters/nodeServer.ts";

const root = mkdtempSync(path.join(tmpdir(), "lite-actions-node-server-"));
const staticDir = path.join(root, "client");
mkdirSync(staticDir);
writeFileSync(path.join(staticDir, "index.html"), "<!doctype html><title>formal</title>");
const server = startNodeServer({
  dbPath: path.join(root, "control.sqlite"), staticDir,
  handler: async (request) => request.url.endsWith("/api/v1/public/overview")
    ? Response.json({formal: true}) : Response.json({code: "NOT_FOUND"}, {status: 404}),
});
try {
  await server.ready;
  const base = "http://127.0.0.1:8788";
  const page = await fetch(`${base}/models`, {headers: {accept: "text/html"}});
  assert.equal(page.status, 200);
  assert.match(await page.text(), /formal/);
  const api = await fetch(`${base}/api/v1/public/overview`);
  assert.deepEqual(await api.json(), {formal: true});
} finally {
  await server.close();
  rmSync(root, {recursive: true, force: true});
}
console.log("node server smoke passed");
