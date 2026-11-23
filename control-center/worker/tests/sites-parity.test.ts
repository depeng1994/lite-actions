import {beforeAll, expect, test} from "vitest";
import worker, {type WorkerEnv} from "../src/index";
import type {StaticAssets} from "../src/storage/sqlStore";
import {bindings, db, migrate, rawDb} from "./db";
import {parityWorkerKeyHash, runPortabilityParitySuite} from "./helpers/portabilitySuite";

beforeAll(migrate);

test("Cloudflare workerd + raw D1Database binding satisfies the full dual-runtime parity contract", async () => {
  // Confirm we pass the raw Cloudflare D1Database binding to worker.fetch so
  // src/index.ts genuinely exercises createD1Store(env.DB as D1Database).
  expect("dbSizeBytes" in rawDb).toBe(false);

  const now = Math.floor(Date.now() / 1000);
  const assets: StaticAssets = {
    async fetch(request: Request): Promise<Response> {
      const url = new URL(request.url);
      if (url.pathname === "/assets/app.js") {
        return new Response("console.log('parity');", {headers: {"Content-Type": "text/javascript; charset=utf-8"}});
      }
      if (url.pathname === "/index.html") {
        return new Response("<!doctype html><title>control-center-parity-shell</title>", {
          headers: {"Content-Type": "text/html; charset=utf-8"},
        });
      }
      return new Response("Not Found", {status: 404});
    },
  };
  const pending: Promise<unknown>[] = [];
  const execution = {
    waitUntil(promise: Promise<unknown>) { pending.push(Promise.resolve(promise)); },
    passThroughOnException() {},
    props: {},
  } as unknown as ExecutionContext;

  const sharedSecret = await parityWorkerKeyHash("test-pepper");
  const serviceEnv = {
    ...bindings,
    ASSETS: assets,
    GITCODE_CANCEL_TOKEN: "parity-gitcode-token",
    WORKER_BLUE_KEY_HASH: sharedSecret,
  };
  const workerEnv: WorkerEnv = {
    ...serviceEnv,
    DB: rawDb,
  };

  await runPortabilityParitySuite({
    name: "workerd/raw-D1",
    store: db,
    env: serviceEnv,
    now,
    async fetch(path, init = {}) {
      const headers = new Headers(init.headers);
      if (init.clientIp) headers.set("CF-Connecting-IP", init.clientIp);
      return worker.fetch(new Request("https://example.test" + path, {
        method: init.method ?? "GET",
        headers,
        body: init.body,
      }), workerEnv, execution);
    },
    async flushBackground() {
      while (pending.length) {
        const batch = pending.splice(0, pending.length);
        await Promise.allSettled(batch);
      }
    },
  });
});
