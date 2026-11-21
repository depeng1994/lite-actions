import {beforeAll, expect, test} from "vitest";
import {authorize} from "../src/middleware/access";
import {hmac} from "../src/utils/crypto";
import type {Context} from "../src/router";
import type {IssuedKey} from "../src/types/api";
import {bindings, db, migrate, request, sessionCookie, signIn} from "./db";

beforeAll(migrate);

test("public cache ignores credential and management is private", async () => {
  const cookie = sessionCookie(await signIn());
  const publicRead = await request("/public/models", {cookie});
  expect(publicRead.headers.get("Cache-Control")).toBe("public, max-age=10");
  expect(await publicRead.text()).toEqual(await (await request("/public/models")).text());
  expect((await request("/public/models", {data: {}})).status).toBe(405);
  for (const path of ["/admin/users", "/admin/tasks/example/move-up", "/admin/cancellations", "/admin/cluster/publish", "/worker/heartbeat"]) {
    const denied = await request(path, {data: {}});
    expect(denied.status, path).toBe(401);
    expect(denied.headers.get("Cache-Control")).toBe("private, no-store");
  }
  expect(await db.prepare("SELECT count(*) AS n FROM users").first("n")).toBe(0);
  const created = await (await request("/admin/users", {cookie, data: {username: "operator"}})).json<IssuedKey>();
  const admin = sessionCookie(await signIn("operator", created.secret_once));
  expect((await request("/admin/users", {cookie: admin})).status).toBe(403);
  const audits = await request("/admin/audits", {cookie: admin});
  expect(audits.status).toBe(200);
  expect(audits.headers.get("Cache-Control")).toBe("private, no-store");
});
test("worker authentication binds identity to token regardless of claimed zone", async () => {
  const token = "test-blue-worker-token";
  const url = new URL("https://example.test/api/v1/worker/heartbeat");
  const config = {...bindings, WORKER_BLUE_KEY_HASH: await hmac(bindings.KEY_PEPPER!, token)};
  const context: Context = {request: new Request(url, {method: "POST",
    body: JSON.stringify({w: "yellow"}), headers: {Authorization: "Bearer " + token}}), env: config, url, params: {}, now: 0};
  await authorize(context);
  expect(context.workerId).toBe("blue");
  expect((await request("/admin/users", {headers: {Authorization: "Bearer " + token}}, config)).status).toBe(401);
  expect((await request("/worker/heartbeat", {headers: {Cookie: sessionCookie(await signIn())}, data: {}}, config)).status).toBe(401);
});
