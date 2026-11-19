import {afterEach, beforeAll, expect, test, vi} from "vitest";
import {trackCancellations} from "../src/services/cancellations";
import {bindings, db, migrate, request, sessionCookie, signIn} from "./db";
import type {Cancellation} from "../src/types/api";

const base = "https://control.example.test/";
const now = Math.floor(Date.now()/1000);
const gc = {...bindings, GITCODE_CANCEL_TOKEN: "test-token"};
beforeAll(migrate);
afterEach(vi.unstubAllGlobals);

test("GitCode stops exactly once, checks attempt before sending, and tracks completed run", async () => {
  let ended = false;
  const routes: string[] = [];
  vi.stubGlobal("fetch", async (url: string, opts: RequestInit) => {
    routes.push(opts.method + " " + url);
    expect(new Headers(opts.headers).get("PRIVATE-TOKEN")).toBe(gc.GITCODE_CANCEL_TOKEN);
    if (url.endsWith("/stop")) {
      ended = true;
      return new Response(null, {status: 202});
    }
    return Response.json({workflow_run_id: "gc-8", attempt: "2", status: ended ? "STOPPED" : "RUNNING"});
  });
  const cookie = sessionCookie(await signIn());
  const data = {provider: "gitcode", repo: "cann/torchtitan-npu", run_id: "gc-8", attempt: 1};
  expect((await request("/admin/cancellations", {cookie, data}, gc)).status).toBe(409);
  expect(routes.filter(path => path.startsWith("POST"))).toHaveLength(0);
  data.attempt = 2;
  const one = await (await request("/admin/cancellations", {cookie, data}, gc)).json<Cancellation>();
  expect(one.status).toBe("SUBMITTED");
  const two = await (await request("/admin/cancellations", {cookie, data}, gc)).json<Cancellation>();
  expect(two.cancel_id).toBe(one.cancel_id);
  expect(routes.filter(path => path.endsWith("/stop"))).toHaveLength(1);
  await trackCancellations({request: new Request(base), url: new URL(base), env: gc, params: {}, now});
  expect(await (await request("/admin/cancellations/" + one.cancel_id, {cookie}, gc)).json()).toMatchObject({
    status: "COMPLETED", platform_conclusion: "stopped",
  });
});

test("GitHub uses an installation token and sends force-cancel once after 120s", async () => {
  const pair = await crypto.subtle.generateKey({name: "RSASSA-PKCS1-v1_5", modulusLength: 2048,
    publicExponent: Uint8Array.from([1, 0, 1]), hash: "SHA-256"}, true, ["sign", "verify"]) as CryptoKeyPair;
  const bytes = new Uint8Array(await crypto.subtle.exportKey("pkcs8", pair.privateKey) as ArrayBuffer);
  const key = "-----BEGIN PRIVATE KEY-----\n" + btoa(String.fromCharCode(...bytes)) + "\n-----END PRIVATE KEY-----";
  const config = {...bindings, GITHUB_APP_ID: "8", GITHUB_APP_PRIVATE_KEY: key};
  const stops: string[] = [];
  vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
    const path = new URL(url).pathname;
    const token = new Headers(init.headers).get("Authorization");
    if (path.endsWith("/installation")) {
      expect(token?.split(".")).toHaveLength(3);
      return path.includes("uninstalled") ? new Response(null, {status: 404}) : Response.json({id: 44});
    }
    if (path.endsWith("/access_tokens")) return Response.json({token: "app-installation", expires_at: new Date((now + 3600) * 1000).toISOString()});
    expect(token).toBe("Bearer app-installation");
    if (init.method === "POST") {
      stops.push(path.split("/").at(-1)!);
      return new Response(null, {status: 202});
    }
    return Response.json({id: 99, run_attempt: 1, status: stops.includes("force-cancel") ? "completed" : "in_progress",
      conclusion: stops.includes("force-cancel") ? "cancelled" : undefined});
  });
  const cookie = sessionCookie(await signIn());
  const data = {provider: "github", repo: "depeng1994/lite-actions", run_id: "99", attempt: 1};
  const result = await (await request("/admin/cancellations", {cookie, data}, config)).json<Cancellation>();
  const context = {request: new Request(base), url: new URL(base), env: config, params: {}};
  await trackCancellations({...context, now: result.submitted_at! + 119});
  expect(stops).toEqual(["cancel"]);
  await trackCancellations({...context, now: result.submitted_at! + 120});
  await trackCancellations({...context, now: result.submitted_at! + 121});
  expect(stops).toEqual(["cancel", "force-cancel"]);
  expect(await db.prepare("SELECT status FROM cancellations WHERE cancel_id=?").bind(result.cancel_id).first("status")).toBe("COMPLETED");
});
