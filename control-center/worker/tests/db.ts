import {env} from "cloudflare:workers";
import {applyD1Migrations} from "cloudflare:test";
import migration from "../migrations/0001_init.sql?raw";
import type {Env} from "../src/router";
import worker from "../src/index";

export const db = (env as unknown as Env).DB;
export const bindings: Env = {DB: db, SUPER_SECRET: "test-super-secret", KEY_PEPPER: "test-pepper"};
export const migrate = () => applyD1Migrations(db, [{
  name: "0001_init.sql", queries: migration.split(";").filter(query => query.trim()),
}], "d1_test_migrations");
export async function request(path: string, opts: {
  method?: string; data?: unknown; cookie?: string; headers?: HeadersInit;
} = {}, config = bindings) {
  const headers = new Headers(opts.headers);
  if (opts.cookie) headers.set("Cookie", opts.cookie);
  return worker.fetch(new Request("https://example.test/api/v1" + path, {
    method: opts.method ?? (opts.data === undefined ? "GET" : "POST"),
    body: opts.data === undefined ? undefined : JSON.stringify(opts.data),
    headers,
  }), config);
}

export async function signIn(username = "super", secret = "test-super-secret", ip = "192.0.2.1") {
  return request("/auth/login", {data: {username, secret}, headers: {"CF-Connecting-IP": ip}});
}
export function sessionCookie(response: Response) {
  return response.headers.get("Set-Cookie")!.split(";")[0];
}
