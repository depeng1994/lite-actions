import {beforeAll, expect, test} from "vitest";
import {db, migrate, request, sessionCookie, signIn} from "./db";
import type {IssuedKey} from "../src/types/api";

beforeAll(migrate);

test("admin lifecycle revokes keys and all sessions and preserves audit", async () => {
  const signed = await signIn();
  const cookie = sessionCookie(signed);
  expect(signed.headers.get("Set-Cookie")).toMatch(/HttpOnly; SameSite=Strict; Max-Age=604800; Secure/);
  const created = await request("/admin/users", {cookie, data: {username: "auditor"}});
  expect(created.status).toBe(201);
  const user = await created.json<IssuedKey>();
  expect(user.secret_once).toMatch(/^la_adm_[A-Za-z0-9_-]{43}$/);
  const adminCookie = sessionCookie(await signIn("auditor", user.secret_once));
  expect((await request("/admin/users", {cookie: adminCookie})).status).toBe(403);
  expect(await (await request("/auth/me", {cookie: adminCookie})).json()).toEqual({
    authenticated: true, username: "auditor", role: "admin",
  });
  const listing = await (await request("/admin/users", {cookie})).text();
  expect(listing).not.toMatch(/secret|key_hash/);
  const reset = await request(`/admin/users/${user.user_id}/reset-key`, {cookie, data: {}});
  expect(reset.status).toBe(200);
  const replacement = await reset.json<IssuedKey>();
  expect(replacement.secret_once).not.toBe(user.secret_once);
  expect(await (await request("/auth/me", {cookie: adminCookie})).json()).toEqual({authenticated: false});
  expect((await signIn("auditor", user.secret_once)).status).toBe(401);
  const newCookie = sessionCookie(await signIn("auditor", replacement.secret_once));
  expect((await request(`/admin/users/${user.user_id}`, {method: "DELETE", cookie})).status).toBe(200);
  expect(await (await request("/auth/me", {cookie: newCookie})).json()).toEqual({authenticated: false});
  expect(await (await request("/admin/audits?limit=2", {cookie})).json()).toMatchObject({
    records: [{action: "DELETE_USER"}, {action: "RESET_USER_KEY"}], next_cursor: expect.any(String),
  });
  const stored = await db.prepare("SELECT key_hash FROM users WHERE user_id=?").bind(user.user_id).first("key_hash");
  expect(stored).toMatch(/^[a-f0-9]{64}$/);
});

test("five failed attempts lock only the username and IP bucket for ten minutes", async () => {
  const ip = "192.0.2.20";
  for (let i = 0; i < 5; i++) expect((await signIn("super", "incorrect", ip)).status).toBe(401);
  const denied = await signIn("super", undefined, ip);
  expect(denied.status).toBe(429);
  expect(Number(denied.headers.get("Retry-After"))).toBeGreaterThan(590);
  expect((await signIn("super", undefined, "192.0.2.21")).status).toBe(200);
  const lowered = Math.floor(Date.now()/1000) - 601;
  await db.prepare("UPDATE login_attempts SET locked_until=?,window_start_at=?").bind(lowered, lowered).run();
  expect((await signIn("super", undefined, ip)).status).toBe(200);
});
