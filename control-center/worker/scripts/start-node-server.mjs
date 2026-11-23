#!/usr/bin/env node
import {existsSync, mkdtempSync, rmSync} from "node:fs";
import {tmpdir} from "node:os";
import path from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {startNodeServer} from "../src/adapters/nodeServer.ts";

const here = path.dirname(fileURLToPath(import.meta.url));
const workerRoot = path.resolve(here, "..");
const prebuiltApp = path.join(workerRoot, "dist", "app.mjs");

let tempBundleDir = null;
let appModulePath = prebuiltApp;
if (!existsSync(appModulePath)) {
  const {buildSync} = await import("esbuild");
  tempBundleDir = mkdtempSync(path.join(tmpdir(), "lite-actions-app-bundle-"));
  appModulePath = path.join(tempBundleDir, "app.mjs");
  buildSync({
    entryPoints: [path.join(workerRoot, "src", "app.ts")],
    bundle: true,
    format: "esm",
    platform: "node",
    outfile: appModulePath,
    logLevel: "silent",
  });
}

const {handleRequest, runMaintenance} = await import(pathToFileURL(appModulePath).href);
if (tempBundleDir) rmSync(tempBundleDir, {recursive: true, force: true});

const dbPath = path.resolve(process.env.DB_PATH || path.join(workerRoot, "control-center.sqlite"));
const staticDir = process.env.STATIC_DIR ? path.resolve(process.env.STATIC_DIR) : path.resolve(workerRoot, "../web/dist");
const port = Number(process.env.PORT || 8788);
const host = process.env.HOST || "127.0.0.1";

const server = startNodeServer({
  dbPath,
  staticDir: existsSync(staticDir) ? staticDir : undefined,
  port,
  host,
  env: {...process.env},
  enableTimers: process.env.DISABLE_TIMERS !== "1",
  handler: (request, env, options) => handleRequest(request, env, options),
  maintenance: (kind, env) => runMaintenance(kind, env),
});

await server.ready;
console.log(`Lite Actions Control Center listening on http://${host}:${port} (db=${dbPath})`);

const shutdown = async () => {
  await server.close();
  process.exit(0);
};
process.once("SIGTERM", shutdown);
process.once("SIGINT", shutdown);
