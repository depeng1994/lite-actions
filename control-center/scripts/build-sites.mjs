#!/usr/bin/env node
import {cp, mkdir, readFile, rm, writeFile} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {execFileSync} from "node:child_process";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const web = path.join(root, "control-center", "web");
const output = path.resolve(process.env.SITES_OUT_DIR || path.join(root, "control-center", "dist"));
const requireOrigin = process.argv.includes("--require-origin");

const projectId = (process.env.SITES_PROJECT_ID || "").trim();
if (!/^[A-Za-z0-9_-]{8,128}$/.test(projectId)) {
  throw new Error(
    "SITES_PROJECT_ID environment variable is required (8-128 chars of [A-Za-z0-9_-]) to build a deployable ChatGPT Sites package",
  );
}

const apiOrigin = (process.env.CONTROL_CENTER_API_ORIGIN || "").trim();
if (requireOrigin || apiOrigin) {
  let parsed;
  try {
    parsed = new URL(apiOrigin);
  } catch {
    throw new Error("CONTROL_CENTER_API_ORIGIN must be a valid http(s) origin URL when deploying or passing --require-origin");
  }
  if (!["https:", "http:"].includes(parsed.protocol) || parsed.pathname !== "/" || parsed.search || parsed.hash) {
    throw new Error("CONTROL_CENTER_API_ORIGIN must be a bare http(s) origin without path, query, or fragment");
  }
}

await rm(output, {recursive: true, force: true});
execFileSync("npm", ["run", "build"], {cwd: web, stdio: "inherit"});
await mkdir(output, {recursive: true});
await cp(path.join(web, "dist"), output, {recursive: true});

const manifestPath = path.join(output, ".openai", "hosting.json");
const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
manifest.project_id = projectId;
manifest.d1 = null;
manifest.r2 = null;
await writeFile(manifestPath, JSON.stringify(manifest, null, 2) + "\n");

for (const required of [
  path.join(output, "client", "index.html"),
  path.join(output, "server", "index.js"),
  manifestPath,
]) await readFile(required);

const server = await readFile(path.join(output, "server", "index.js"), "utf8");
if (server.includes("workers.dev") || server.includes("chatgpt.site")) {
  throw new Error("Sites server bundle must receive CONTROL_CENTER_API_ORIGIN at runtime");
}
if (manifest.r2 !== null || manifest.d1 !== null) {
  throw new Error("Sites frontend proxy package must keep d1=null and r2=null");
}
if (!manifest.project_id) {
  throw new Error("Sites manifest project_id must be non-empty");
}

if (apiOrigin) {
  // Verify runtime env.CONTROL_CENTER_API_ORIGIN proxy forwarding without baking the origin into the bundle.
  const {default: sitesWorker} = await import(pathToFileURL(path.join(output, "server", "index.js")).href);
  const originalFetch = globalThis.fetch;
  let forwardedHref = null;
  globalThis.fetch = async (req) => {
    forwardedHref = new URL(req.url).href;
    return Response.json({ok: true});
  };
  try {
    const res = await sitesWorker.fetch(
      new Request("https://site.example/api/v1/public/overview"),
      {CONTROL_CENTER_API_ORIGIN: apiOrigin},
    );
    if (res.status !== 200 || forwardedHref !== `${apiOrigin.replace(/\/$/, "")}/api/v1/public/overview`) {
      throw new Error(`Sites proxy verification failed (status=${res.status}, target=${forwardedHref})`);
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
}

console.log(`Built ChatGPT Sites package at ${path.relative(root, output)} (project_id=${projectId})`);
