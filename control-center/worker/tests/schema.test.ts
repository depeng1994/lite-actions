import {beforeAll, expect, test} from "vitest";
import {logicalModel, modelMap} from "../src/modelMap";
import {db, migrate} from "./db";

beforeAll(migrate);

test("migrations preserve the history index and API columns", async () => {
  const index = await db.prepare("SELECT sql FROM sqlite_schema WHERE name='idx_task_index_model_page'").first<string>("sql");
  expect(index).toContain("finished_at DESC,task_uid DESC");
  const columns = await db.prepare("PRAGMA table_info(device_group_states)").all<{name: string}>();
  expect(columns.results.map(column => column.name)).toEqual(expect.arrayContaining([
    "visibility_state", "consecutive_failures", "last_online_at", "last_probe_round",
  ]));
  const activeColumns = await db.prepare("PRAGMA table_info(tasks_active)").all<{name: string}>();
  const historyColumns = await db.prepare("PRAGMA table_info(task_index)").all<{name: string}>();
  expect(activeColumns.results.map(column => column.name)).toEqual(expect.arrayContaining(["submitted_at", "workflow_name", "run_number", "display_title"]));
  expect(historyColumns.results.map(column => column.name)).toEqual(expect.arrayContaining(["submitted_at", "workflow_name", "started_at", "run_number", "display_title"]));
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
