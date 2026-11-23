import {execFileSync} from "node:child_process";
import {mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync} from "node:fs";
import {tmpdir} from "node:os";
import path from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {expect, test} from "vitest";
import {handleRequest} from "../src/app";
import {startNodeServer} from "../src/adapters/nodeServer";
import {toArrayBuffer} from "../src/storage/sqlStore";
import {parityWorkerKeyHash, runPortabilityParitySuite} from "./helpers/portabilitySuite";

const workerRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(workerRoot, "../..");

function walkFiles(dir: string, predicate: (file: string) => boolean): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, {withFileTypes: true})) {
    if (entry.name === "node_modules" || entry.name === "dist" || entry.name === ".git") continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walkFiles(full, predicate));
    else if (predicate(full)) out.push(full);
  }
  return out;
}

const CORE_FORBIDDEN: Array<{label: string; pattern: RegExp}> = [
  {label: "crypto.subtle.timingSafeEqual", pattern: /\bcrypto\.subtle\.timingSafeEqual\b/},
  {label: "CF-Connecting-IP", pattern: /CF-Connecting-IP/},
  {label: "caches.default", pattern: /\bcaches\.default\b/},
  {label: "HTMLRewriter", pattern: /\bHTMLRewriter\b/},
  {label: "WebSocketPair", pattern: /\bWebSocketPair\b/},
  {label: "DurableObject", pattern: /\bDurableObject\b/},
  {label: "R2Bucket", pattern: /\bR2Bucket\b/},
  {label: "KVNamespace", pattern: /\bKVNamespace\b/},
  {label: "@cloudflare/* or cloudflare:* import", pattern: /from\s+["'](?:@cloudflare\/|cloudflare:)/},
  {label: "node:* or better-sqlite3 import", pattern: /from\s+["'](?:node:|better-sqlite3)/},
  {label: "D1Database / D1PreparedStatement", pattern: /\b(?:D1Database|D1PreparedStatement)\b/},
  {label: "ExecutionContext / ScheduledController / Fetcher", pattern: /\b(?:ExecutionContext|ScheduledController|Fetcher)\b/},
  {label: "hardcoded workers.dev / chatgpt.site", pattern: /https?:\/\/[^\s"'`]*(?:workers\.dev|chatgpt\.site)/},
  {label: "private host path", pattern: /\/Users\/|lite-actions-demo-handoff|lite-actions-control-center-demo/},
];

const CONFIG_FORBIDDEN: Array<{label: string; pattern: RegExp}> = [
  {label: "paid CF bindings (R2/KV/DO/Queues)", pattern: /\[\[(?:r2_buckets|kv_namespaces|durable_objects|queues)\]\]/},
  {label: "hardcoded personal project_id", pattern: /appgprj_[a-f0-9]+/i},
];

export function scanLines(relativePath: string, content: string, rules: Array<{label: string; pattern: RegExp}>): string[] {
  const findings: string[] = [];
  const lines = content.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    for (const rule of rules) {
      if (rule.pattern.test(lines[i])) {
        findings.push(`${relativePath}:${i + 1}: forbidden ${rule.label}`);
      }
    }
  }
  return findings;
}

test("Layer 1: static vendor-lock, paid-binding, and hardcoded-origin guard", () => {
  const findings: string[] = [];
  const srcRoot = path.join(workerRoot, "src");
  for (const file of walkFiles(srcRoot, name => name.endsWith(".ts"))) {
    const rel = path.relative(srcRoot, file).split(path.sep).join("/");
    if (rel === "index.ts" || rel.startsWith("adapters/")) continue;
    findings.push(...scanLines(`control-center/worker/src/${rel}`, readFileSync(file, "utf8"), CORE_FORBIDDEN));
  }

  const wranglerPath = path.join(workerRoot, "wrangler.toml");
  findings.push(...scanLines("control-center/worker/wrangler.toml", readFileSync(wranglerPath, "utf8"), CONFIG_FORBIDDEN));

  const hostingPath = path.join(repoRoot, "control-center/web/.openai/hosting.json");
  const hostingText = readFileSync(hostingPath, "utf8");
  findings.push(...scanLines("control-center/web/.openai/hosting.json", hostingText, CONFIG_FORBIDDEN));
  const hosting = JSON.parse(hostingText);
  expect(hosting.d1).toBeNull();
  expect(hosting.r2).toBeNull();

  const webSrcRoot = path.join(repoRoot, "control-center/web/src");
  const webRules: Array<{label: string; pattern: RegExp}> = [
    {label: "hardcoded backend domain", pattern: /https?:\/\/[^\s"'`]*(?:workers\.dev|chatgpt\.site|chatgpt\.com|openai\.com)/},
    {label: "private host path", pattern: /\/Users\/|lite-actions-demo-handoff|lite-actions-control-center-demo/},
    {label: "hardcoded personal project_id", pattern: /appgprj_[a-f0-9]+/i},
  ];
  for (const file of walkFiles(webSrcRoot, name => /\.(?:js|jsx|ts|tsx)$/.test(name))) {
    const rel = path.relative(repoRoot, file).split(path.sep).join("/");
    findings.push(...scanLines(rel, readFileSync(file, "utf8"), webRules));
  }
  for (const scriptRel of ["control-center/scripts/build-sites.mjs", "control-center/worker/scripts/sync-frontend.mjs"]) {
    findings.push(...scanLines(scriptRel, readFileSync(path.join(repoRoot, scriptRel), "utf8"), [
      {label: "private host path", pattern: /\/Users\/|lite-actions-demo-handoff|lite-actions-control-center-demo/},
      {label: "hardcoded personal project_id", pattern: /appgprj_[a-f0-9]+/i},
    ]));
  }
  expect(findings).toEqual([]);
});

test("Layer 1b: negative self-checks catch vendor lock-in, paid bindings, and hardcoded URLs with file:line", () => {
  expect(scanLines("services/bad.ts", "const ip = req.headers.get('CF-Connecting-IP');", CORE_FORBIDDEN)[0])
    .toContain("services/bad.ts:1");
  expect(scanLines("services/bad.ts", "\ncrypto.subtle.timingSafeEqual(a, b);", CORE_FORBIDDEN)[0])
    .toContain("services/bad.ts:2");
  expect(scanLines("services/bad.ts", "import fs from 'node:fs';", CORE_FORBIDDEN)[0])
    .toContain("services/bad.ts:1");
  expect(scanLines("wrangler.toml", "[[r2_buckets]]\nbinding='R2'", CONFIG_FORBIDDEN)[0])
    .toContain("wrangler.toml:1");
  expect(scanLines("web/src/bad.js", "fetch('https://demo.workers.dev/api')", CORE_FORBIDDEN)[0])
    .toContain("web/src/bad.js:1");
});

test("Layer 2: Node 24 + node:sqlite + nodeServer passes the exact same dual-runtime parity suite", async () => {
  const scratch = mkdtempSync(path.join(tmpdir(), "lite-actions-node-parity-"));
  const dbPath = path.join(scratch, "control.sqlite");
  const staticDir = path.join(scratch, "client");
  mkdirSync(path.join(staticDir, "assets"), {recursive: true});
  writeFileSync(path.join(staticDir, "index.html"), "<!doctype html><title>control-center-parity-shell</title>");
  writeFileSync(path.join(staticDir, "assets", "app.js"), "console.log('parity');");

  const {DatabaseSync} = await import("node:sqlite");
  const initDb = new DatabaseSync(dbPath);
  initDb.exec("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;");
  const migrationsDir = path.join(workerRoot, "migrations");
  for (const file of readdirSync(migrationsDir).filter(name => name.endsWith(".sql")).sort()) {
    initDb.exec(readFileSync(path.join(migrationsDir, file), "utf8"));
  }
  initDb.close();

  const workerBlueHash = await parityWorkerKeyHash("test-pepper");
  const baseEnv = {
    SUPER_SECRET: "test-super-secret",
    KEY_PEPPER: "test-pepper",
    DEV_INSECURE_COOKIE: "1",
    TRUST_PROXY_HEADERS: "1",
    GITCODE_CANCEL_TOKEN: "parity-gitcode-token",
    WORKER_BLUE_KEY_HASH: workerBlueHash,
  };
  const now = Math.floor(Date.now() / 1000);
  const server = startNodeServer({
    dbPath,
    staticDir,
    port: 0,
    env: baseEnv,
    handler: (req, runtimeEnv, opts) => handleRequest(req, runtimeEnv as any, opts),
  });
  try {
    await server.ready;
    const address = server.server.address();
    if (!address || typeof address === "string") throw new Error("Server address unavailable");
    const base = `http://127.0.0.1:${address.port}`;
    await runPortabilityParitySuite({
      name: "Node24/SQLite",
      store: server.store,
      env: {...baseEnv, DB: server.store},
      now,
      async fetch(pathname, init = {}) {
        const headers: Record<string, string> = {...init.headers};
        if (init.clientIp) headers["x-forwarded-for"] = init.clientIp;
        return globalThis.fetch(base + pathname, {
          method: init.method ?? "GET",
          headers,
          body: init.body,
        });
      },
      flushBackground: server.flushBackground,
    });
  } finally {
    await server.close();
    rmSync(scratch, {recursive: true, force: true});
  }
});

test("Layer 3: ChatGPT Sites frontend-proxy package requires injected project_id and proxies runtime API origin", async () => {
  const scratch = mkdtempSync(path.join(tmpdir(), "lite-actions-sites-pkg-"));
  const outDir = path.join(scratch, "dist");
  const buildScript = path.join(repoRoot, "control-center/scripts/build-sites.mjs");
  try {
    // Missing or empty SITES_PROJECT_ID must fail fast rather than emitting an undeployable manifest.
    expect(() => execFileSync(process.execPath, [buildScript], {
      env: {...process.env, SITES_OUT_DIR: outDir, SITES_PROJECT_ID: ""},
      stdio: "pipe",
    })).toThrow(/SITES_PROJECT_ID/);

    // --require-origin must reject missing CONTROL_CENTER_API_ORIGIN.
    expect(() => execFileSync(process.execPath, [buildScript, "--require-origin"], {
      env: {...process.env, SITES_OUT_DIR: outDir, SITES_PROJECT_ID: "sites_injected_test_01", CONTROL_CENTER_API_ORIGIN: ""},
      stdio: "pipe",
    })).toThrow(/CONTROL_CENTER_API_ORIGIN/);

    // Valid deployment-time injection succeeds and writes non-empty project_id with d1=null, r2=null.
    execFileSync(process.execPath, [buildScript, "--require-origin"], {
      env: {
        ...process.env,
        SITES_OUT_DIR: outDir,
        SITES_PROJECT_ID: "sites_injected_test_01",
        CONTROL_CENTER_API_ORIGIN: "https://control-upstream.example",
      },
      stdio: "pipe",
    });
    const manifest = JSON.parse(readFileSync(path.join(outDir, ".openai/hosting.json"), "utf8"));
    expect(manifest).toEqual({project_id: "sites_injected_test_01", d1: null, r2: null});

    const clientAssetsDir = path.join(outDir, "client/assets");
    for (const file of readdirSync(clientAssetsDir).filter(name => name.endsWith(".js"))) {
      const bundle = readFileSync(path.join(clientAssetsDir, file), "utf8");
      expect(bundle).not.toMatch(/workers\.dev|chatgpt\.site|appgprj_/);
    }

    const {default: sitesWorker} = await import(pathToFileURL(path.join(outDir, "server/index.js")).href);
    const rootResp = await sitesWorker.fetch(new Request("https://site.example/"), {});
    expect(rootResp.status).toBe(200);
    expect(await rootResp.text()).toContain("<!doctype html>");

    const spaResp = await sitesWorker.fetch(new Request("https://site.example/models", {headers: {accept: "text/html"}}), {});
    expect(spaResp.status).toBe(200);

    const unconfigured = await sitesWorker.fetch(new Request("https://site.example/api/v1/public/overview"), {});
    expect(unconfigured.status).toBe(503);
    expect(await unconfigured.json()).toEqual({code: "CONTROL_CENTER_API_NOT_CONFIGURED"});

    const originalFetch = globalThis.fetch;
    const proxiedTargets: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const target = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      proxiedTargets.push(target);
      return Response.json({ok: true, page_refresh_sec: 30});
    }) as typeof fetch;
    try {
      const proxied = await sitesWorker.fetch(
        new Request("https://site.example/api/v1/public/overview"),
        {CONTROL_CENTER_API_ORIGIN: "https://control-upstream.example"},
      );
      expect(proxied.status).toBe(200);
      expect(await proxied.json()).toEqual({ok: true, page_refresh_sec: 30});
      expect(proxiedTargets).toEqual(["https://control-upstream.example/api/v1/public/overview"]);
    } finally {
      globalThis.fetch = originalFetch;
    }

    const badApi = await sitesWorker.fetch(new Request("https://site.example/api/unknown", {headers: {accept: "text/html"}}), {});
    expect(badApi.status).toBe(404);
    expect(badApi.headers.get("content-type")).toContain("application/json");
  } finally {
    rmSync(scratch, {recursive: true, force: true});
  }
});

test("Layer 4: BLOB normalization handles ArrayBuffer, views, byte arrays, and rejects invalid inputs", () => {
  const bytes = [0, 1, 127, 255];
  expect([...new Uint8Array(toArrayBuffer(bytes))]).toEqual(bytes);
  const view = new Uint8Array(bytes).subarray(1, 3);
  expect([...new Uint8Array(toArrayBuffer(view))]).toEqual([1, 127]);
  expect([...new Uint8Array(toArrayBuffer(view.buffer))]).toEqual(bytes);
  expect(() => toArrayBuffer("not-a-blob")).toThrow(TypeError);
});
