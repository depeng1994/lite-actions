import {env} from "cloudflare:workers";
import {applyD1Migrations} from "cloudflare:test";
import migration from "../migrations/0001_init.sql?raw";
import healthMigration from "../migrations/0002_v15_health.sql?raw";
import drainMigration from "../migrations/0003_v15_cluster_drain.sql?raw";
import priorityMigration from "../migrations/0004_v15_workflow_priority.sql?raw";
import metadataMigration from "../migrations/0005_v15_workflow_metadata.sql?raw";
import workflowRunsMigration from "../migrations/0006_v15_workflow_runs.sql?raw";
import taskStartedAtMigration from "../migrations/0007_v15_task_started_at.sql?raw";
import runNumberMigration from "../migrations/0008_v15_run_number.sql?raw";
import runDisplayTitleMigration from "../migrations/0009_v15_run_display_title.sql?raw";
import type {Env} from "../src/router";
import worker from "../src/index";

export const db = (env as unknown as Env).DB;
export const bindings: Env = {DB: db, SUPER_SECRET: "test-super-secret", KEY_PEPPER: "test-pepper"};
export const migrate = () => applyD1Migrations(db, [{
  name: "0001_init.sql", queries: migration.split(";").filter(query => query.trim()),
}, {
  name: "0002_v15_health.sql", queries: healthMigration.split(";").filter(query => query.trim()),
}, {
  name: "0003_v15_cluster_drain.sql", queries: drainMigration.split(";").filter(query => query.trim()),
}, {
  name: "0004_v15_workflow_priority.sql", queries: priorityMigration.split(";").filter(query => query.trim()),
}, {
  name: "0005_v15_workflow_metadata.sql", queries: metadataMigration.split(";").filter(query => query.trim()),
}, {
  name: "0006_v15_workflow_runs.sql", queries: workflowRunsMigration.split(";").filter(query => query.trim()),
}, {
  name: "0007_v15_task_started_at.sql", queries: taskStartedAtMigration.split(";").filter(query => query.trim()),
}, {
  name: "0008_v15_run_number.sql", queries: runNumberMigration.split(";").filter(query => query.trim()),
}, {
  name: "0009_v15_run_display_title.sql", queries: runDisplayTitleMigration.split(";").filter(query => query.trim()),
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
