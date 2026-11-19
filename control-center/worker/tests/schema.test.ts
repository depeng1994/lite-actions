import { env } from "cloudflare:workers";
import { applyD1Migrations } from "cloudflare:test";
import { expect, test } from "vitest";
import migrations from "../migrations/0001_init.sql?raw";
import healthMigration from "../migrations/0002_v15_health.sql?raw";
import drainMigration from "../migrations/0003_v15_cluster_drain.sql?raw";
import { logicalModel, modelMap } from "../src/modelMap";
import type { Env } from "../src/router";

const db = (env as unknown as Env).DB;

test("migrations create all 19 project tables and history indexes", async () => {
  await applyD1Migrations(db, [
    {name: "0001_init.sql", queries: migrations.split(";").filter(query => query.trim())},
    {name: "0002_v15_health.sql", queries: healthMigration.split(";").filter(query => query.trim())},
    {name: "0003_v15_cluster_drain.sql", queries: drainMigration.split(";").filter(query => query.trim())},
  ], "d1_test_migrations");
  const {results} = await db.prepare("SELECT name FROM sqlite_schema WHERE type='table'").all<{name: string}>();
  expect(results.map(table => table.name).filter(name => !/^(d1_|sqlite_|_cf_)/.test(name))).toHaveLength(20);
  const index = await db.prepare("SELECT sql FROM sqlite_schema WHERE name='idx_task_index_model_page'").first<string>("sql");
  expect(index).toContain("finished_at DESC,task_uid DESC");
  const columns = await db.prepare("PRAGMA table_info(device_group_states)").all<{name: string}>();
  expect(columns.results.map(column => column.name)).toEqual(expect.arrayContaining([
    "visibility_state", "consecutive_failures", "last_online_at", "last_probe_round",
  ]));
});

test("configured repositories form logical model tabs with a default", () => {
  expect(logicalModel("github", "cann-framework/torchtitan-npu")).toBe("torchtitan-npu");
  expect(logicalModel("gitcode", "cann/torchtitan-npu")).toBe("torchtitan-npu");
  expect(logicalModel("github", "depeng1994/lite-actions")).toBe("lite-actions");
  expect(logicalModel("github", "new/Project")).toBe("project");
  expect(modelMap.default_model).toBe("vllm-ascend");
  expect(modelMap.models.find(model => model.slug === "lite-actions")?.repos[0].repo_scope).toBe("个人仓");
  expect(modelMap.models.find(model => model.slug === "vllm-ascend")?.repos[0].repo_scope).toBe("组织仓");
});
