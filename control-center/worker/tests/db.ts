import {DatabaseSync} from "node:sqlite";
import {mkdtempSync, readFileSync, readdirSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {handleRequest} from "../src/app";
import {createNodeSqliteStore} from "../src/adapters/nodeSqlite";
import type {Env} from "../src/router";

const directory = mkdtempSync(join(tmpdir(), "lite-actions-control-center-test-"));
const sqlitePath = join(directory, "control-center.sqlite");
const rawDb = new DatabaseSync(sqlitePath);
rawDb.exec("PRAGMA journal_mode = WAL; PRAGMA busy_timeout = 5000; PRAGMA foreign_keys = ON;");
export const db = createNodeSqliteStore(sqlitePath);
export const bindings: Env = {DB: db, SUPER_SECRET: "test-super-secret", KEY_PEPPER: "test-pepper"};
export const workerBindings = bindings;

let migrated = false;
export async function migrate() {
  if (migrated) return;
  for (const file of readdirSync(new URL("../migrations/", import.meta.url)).filter(name => name.endsWith(".sql")).sort()) {
    rawDb.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), "utf8"));
  }
  migrated = true;
}

export async function request(path: string, opts: {
  method?: string; data?: unknown; cookie?: string; headers?: HeadersInit;
} = {}, config: Env = bindings) {
  const headers = new Headers(opts.headers);
  if (opts.cookie) headers.set("Cookie", opts.cookie);
  const response = await handleRequest(new Request("https://example.test/api/v1" + path, {
    method: opts.method ?? (opts.data === undefined ? "GET" : "POST"),
    body: opts.data === undefined ? undefined : JSON.stringify(opts.data),
    headers,
  }), config, {clientIp: headers.get("X-Test-Client-IP") || "192.0.2.1"});
  return response;
}

export async function signIn(username = "super", secret = "test-super-secret", ip = "192.0.2.1") {
  return request("/auth/login", {data: {username, secret}, headers: {"X-Test-Client-IP": ip}});
}

export function sessionCookie(response: Response) {
  return response.headers.get("Set-Cookie")!.split(";")[0];
}
