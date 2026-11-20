import {beforeAll, expect, test} from "vitest";
import {sha256} from "../src/utils/crypto";
import {bindings, db, migrate, request, sessionCookie, signIn} from "./db";

beforeAll(migrate);

test("super can register a SHA-256 worker credential without echoing the hash", async () => {
  await db.prepare("DELETE FROM worker_credentials").run();
  const cookie = sessionCookie(await signIn());
  const token = "yellow-fallback-token-256";
  const digest = await sha256(token);
  const registered = await request("/admin/worker-credentials", {
    cookie,
    data: {worker_id: "yellow", scheme: "sha256", hash: digest},
  });
  expect(registered.status).toBe(200);
  expect(await registered.json()).toEqual({worker_id: "yellow", scheme: "sha256", created_at: expect.any(Number)});
  expect(await registered.text()).resolves.not.toContain(digest);

  const heartbeat = await request("/worker/heartbeat", {
    method: "POST",
    headers: {Authorization: `Bearer ${token}`},
    data: {w: "yellow", i: "deadbeef", rev: 0, sha: "", ok: 1, act: 0, q: 0, eu: 0},
  });
  expect(heartbeat.status).toBe(200);

  const claimedBlue = await request("/worker/heartbeat", {
    method: "POST",
    headers: {Authorization: `Bearer ${token}`},
    data: {w: "blue", i: "deadbeef", rev: 0, sha: "", ok: 1, act: 0, q: 0, eu: 0},
  });
  expect(claimedBlue.status).toBe(403);
  expect(await claimedBlue.json()).toMatchObject({code: "WORKER_MISMATCH"});

  const random = await request("/worker/heartbeat", {
    method: "POST",
    headers: {Authorization: "Bearer random-unregistered-token"},
    data: {w: "yellow", i: "deadbeef", rev: 0, sha: "", ok: 1, act: 0, q: 0, eu: 0},
  });
  expect(random.status).toBe(401);
  expect(await random.json()).toMatchObject({code: "WORKER_REQUIRED"});
});

test("ambiguous SHA-256 registrations fail closed", async () => {
  await db.prepare("DELETE FROM worker_credentials").run();
  const digest = await sha256("ambiguous-token");
  await db.batch([
    db.prepare("INSERT INTO worker_credentials(worker_id,scheme,hash,created_at) VALUES (?,?,?,?)").bind("blue", "sha256", digest, 1),
    db.prepare("INSERT INTO worker_credentials(worker_id,scheme,hash,created_at) VALUES (?,?,?,?)").bind("yellow", "sha256", digest, 1),
  ]);
  const response = await request("/worker/heartbeat", {
    method: "POST",
    headers: {Authorization: "Bearer ambiguous-token"},
    data: {w: "yellow", i: "deadbeef", rev: 0, sha: "", ok: 1, act: 0, q: 0, eu: 0},
  });
  expect(response.status).toBe(401);
  expect(await response.json()).toMatchObject({code: "WORKER_REQUIRED"});
});
