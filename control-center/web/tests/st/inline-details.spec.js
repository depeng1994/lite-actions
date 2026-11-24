import { expect, test } from "@playwright/test";

const time = Date.parse("2026-11-24T08:00:00Z") / 1000;
const ago = (minutes) => time - minutes * 60;
const host = "192.0.2.1:22";
const pool = "blue_pool";
const occupancy = [
  { started_at: ago(240), last_busy_at: ago(180), ended_at: ago(178), close_reason: "idle" },
  { started_at: ago(8), last_busy_at: time, ended_at: null, close_reason: null },
];
const health = { status: "ONLINE", missed_count: 0, last_online_at: time };
const groups = Array.from({ length: 4 }, (_, index) => ({
  devices: [index * 2, index * 2 + 1],
  state: index === 0 ? "UNKNOWN_BUSY" : "LEASED",
  health,
  occupancy_blocks: index === 0 ? occupancy : [],
}));
const node = { node_ref: "n_blue", host_id: host, health, device_groups: groups };
const yellow = { node_ref: "n_yellow", host_id: "192.0.2.2:22", health,
  device_groups: [{ devices: [0, 1], state: "CONFIRMED_IDLE", health }] };
const worker = { worker_id: "blue", applied_rev: 1, offline: false };
const timeline = {
  workers: [
    { ...worker, pools: [{ pool_name: pool, nodes: [node] }] },
    { worker_id: "yellow", pools: [{ pool_name: "yellow_pool", nodes: [yellow] }] },
  ],
};

function task(label, id, phase, devices, started, reserved = started) {
  return {
    task_uid: id, run_id: "1", attempt: 1, provider: "github", repo: "test/project",
    logical_model: "lite-actions", workflow_name: "Smoke", label, phase, kind: "npu_smoke",
    worker_id: "blue", target_pool: pool, req_nodes: 1, req_devices: 2,
    started_at: started, reserved_at: reserved,
    allocation: devices ? [{ node_ref: node.node_ref, host_id: host, devices }] : [],
    allocated_cards: devices?.length ?? 0,
  };
}

const queued = task("排期用例", "queued", "QUEUED");
const waiting = task("等待外部释放用例", "waiting", "QUEUED");
const running = task("运行用例", "running", "RUNNING", [2, 3], ago(35), ago(36));
const preparing = task("准备中用例", "preparing", "STAGING", [4, 5], ago(30));
const cleaning = task("清理中用例", "cleaning", "CLEANING", [6, 7], ago(25));
const completed = { ...task("历史用例", "completed", null, [2, 3], ago(150), ago(151)),
  run_id: "2", status: "PASS", finished_at: ago(120), cleanup_verified: true, exit_code: 0 };
const active = [queued, waiting, running, preparing, cleaning];

async function setup(page, authenticated = false) {
  await page.clock.setFixedTime(new Date(time * 1000));
  const allocationGroups = groups.map(({ occupancy_blocks, ...group }) => group);
  const responses = {
    overview: {},
    timeline: {
      ...timeline,
      history_tasks: [completed],
      projections: [
        { task_uid: queued.task_uid, projection_status: "SCHEDULED", est_start: ago(-10), est_end: ago(-45) },
        { task_uid: waiting.task_uid, projection_status: "WAITING_EXTERNAL" },
      ],
    },
    models: { models: [{ slug: "lite-actions" }] },
    workflows: { active: [{ ...queued, cases: active }], history: [] },
    cluster: {
      revision: 1, workers: [worker], history: [{ revision: 1 }],
      config: {
        [pool]: { owner: "blue", nodes: [
          { host_id: host, health, allocation_groups: allocationGroups },
          { host_id: "192.0.2.3:22", health, allocation_groups: [] },
        ] },
        yellow_pool: { owner: "yellow", nodes: [
          { host_id: yellow.host_id, health, allocation_groups: yellow.device_groups },
        ] },
      },
    },
    audit: { records: [] },
  };
  const projected = responses.timeline.projections[0];
  responses.timeline.workers = timeline.workers.map((zone) => zone.worker_id !== "blue" ? zone : {
    ...zone, pools: [{ pool_name: pool, nodes: [{
      ...node, device_groups: [{ ...groups[0], dashed_blocks: [projected] }, ...groups.slice(1)],
    }] }],
  });
  await page.route("**/api/v1/**", async (route) => {
    const [, scope, resource, id] = new URL(route.request().url()).pathname.slice("/api/v1".length).split("/");
    if (scope === "auth" && resource === "me") {
      return route.fulfill({ json: { authenticated, username: "tester", role: "admin" } });
    }
    const data = resource === "tasks" ? [...active, completed].find((entry) => entry.task_uid === id) : responses[resource];
    await route.fulfill(data ? { json: data } : { status: 404, json: { code: "NO_FIXTURE" } });
  });
}

const pools = { panel: "#timeline-detail", above: ".timeline-board", below: ".queue-section" };
const cluster = { panel: "#cluster-detail", above: ".cluster-table", below: '[aria-label="历史配置版本"]' };

async function expectInline(page, source, placement, title) {
  await source.click();
  const panel = page.locator(placement.panel);
  await expect(panel.getByRole("heading", { name: title, exact: true })).toBeVisible();
  await expect(page.locator("dialog[open]")).toHaveCount(0);
  await expect(panel).toHaveCSS("border-top-style", "solid");
  await expect(panel.locator(".inline-detail-columns")).toHaveCSS("display", "grid");
  expect(await panel.evaluate((element, { above, below }) => {
    const frame = element.getBoundingClientRect();
    return frame.top >= document.querySelector(above).getBoundingClientRect().bottom
      && frame.bottom <= document.querySelector(below).getBoundingClientRect().top;
  }, placement)).toBe(true);
  return panel;
}

async function expectTaskTimes(panel, start, end) {
  await expect(panel.getByText("占用开始时间", { exact: true }).locator("..").locator("dd")).toHaveText(start);
  await expect(panel.getByText("占用结束时间", { exact: true }).locator("..").locator("dd")).toHaveText(end);
}

async function expectOccupancy(panel, start, end) {
  const cells = panel.locator("tbody tr").filter({ hasText: start }).getByRole("cell");
  await expect(cells.nth(0)).toHaveText(start);
  await expect(cells.nth(1)).toHaveText(end);
}

test("Pools opens all task and occupancy entry points in the same detail slot", async ({ page }) => {
  await setup(page);
  await page.goto("/#/pools");
  const group = "192.0.2.1:22 · 设备组 0–1";
  await test.step("三种设备状态图标同行显示", async () => {
    for (const width of [1600, 960]) {
      await page.setViewportSize({ width, height: 1080 });
      for (const [device, state, tooltip] of [
        ["192.0.2.1:22 设备组 0–1", "unknown", "当前未知占用"],
        ["192.0.2.1:22 设备组 2–3", "leased", "当前实际租约"],
        ["192.0.2.2:22 设备组 0–1", "idle", "当前已确认空闲"],
      ]) {
        const leaf = page.locator(".tree-leaf").getByRole("button").or(page.locator(".tree-leaf"))
          .filter({ hasText: device });
        const icon = leaf.locator(`.resource-occupancy.occupancy-${state}`);
        await expect(icon).toHaveAttribute("title", tooltip);
        await expect(leaf).toHaveAccessibleName(new RegExp(tooltip));
        expect(await leaf.evaluate((element) => {
          const name = element.querySelector(".resource-group-name").getBoundingClientRect();
          const cards = element.querySelector(".resource-group-size").getBoundingClientRect();
          const status = element.querySelector(".resource-occupancy").getBoundingClientRect();
          return status.left >= cards.right && Math.abs((name.top + name.bottom) - (status.top + status.bottom)) < 2
            && status.right <= element.getBoundingClientRect().right;
        })).toBe(true);
      }
    }
    await page.setViewportSize({ width: 1600, height: 1080 });
    await expect(page.locator(".occupancy-marker.occupancy-leased")).toHaveCount(0);
  });
  for (const [label, start, end] of [
    ["运行用例", "2026-11-24 15:24:00", "—"],
    ["准备中用例", "2026-11-24 15:30:00", "—"],
    ["清理中用例", "2026-11-24 15:35:00", "—"],
    ["历史用例", "2026-11-24 13:29:00", "2026-11-24 14:00:00"],
    ["排期用例", "—", "—"],
  ]) {
    await test.step(label, async () => {
      const bar = page.locator(".lease").and(page.getByRole("button", { name: new RegExp(label) }));
      await expectTaskTimes(await expectInline(page, bar, pools, label), start, end);
    });
  }
  await test.step("排队表中的用例", async () => {
    const entry = page.locator(".queue-table").getByRole("button", { name: waiting.label });
    await expectTaskTimes(await expectInline(page, entry, pools, waiting.label), "—", "—");
  });
  await test.step("外部占用区间", async () => {
    const [completed, active] = await page.locator(".occupancy-history").all();
    const closedPanel = await expectInline(page, completed, pools, group);
    await expectOccupancy(closedPanel, "2026-11-24 12:00:00", "2026-11-24 13:02:00");
    await expect(closedPanel.getByRole("row")).toHaveCount(2);
    const openPanel = await expectInline(page, active, pools, group);
    await expectOccupancy(openPanel, "2026-11-24 15:52:00", "—");
  });
  await test.step("当前状态标记和资源名称", async () => {
    const panel = await expectInline(page, page.locator(".occupancy-marker.occupancy-unknown"), pools, group);
    await expectOccupancy(panel, "2026-11-24 12:00:00", "2026-11-24 13:02:00");
    await expectOccupancy(panel, "2026-11-24 15:52:00", "—");
    const idlePanel = await expectInline(page, page.locator(".occupancy-marker.occupancy-idle"), pools, "192.0.2.2:22 · 设备组 0–1");
    await expect(idlePanel.getByText("暂无外部占用记录")).toBeVisible();
    await expectInline(page, page.locator(".tree-leaf").filter({ hasText: /192\.0\.2\.1:22.*0–1/ }), pools, group);
    await panel.getByRole("button", { name: "关闭" }).click();
    await expect(panel).toHaveCount(0);
  });
});

test("cluster reuses the detail slot checks for Pool, node and device groups", async ({ page }) => {
  await setup(page, true);
  await page.goto("/#/cluster");
  const table = page.locator(".cluster-table");
  const poolPanel = await expectInline(page, table.getByRole("button", { name: "Pool blue_pool" }), cluster, "Blue / blue_pool");
  await expectOccupancy(poolPanel, "2026-11-24 15:52:00", "—");
  const nodePanel = await expectInline(page, table.getByRole("button", { name: "192.0.2.3:22", exact: true }), cluster, "192.0.2.3:22");
  await expect(nodePanel.getByText("暂无外部占用记录")).toBeVisible();
  const group = table.getByRole("button", { name: "192.0.2.1:22 设备组 0–1" });
  const panel = await expectInline(page, group, cluster, "192.0.2.1:22 · 设备组 0–1");
  await expectOccupancy(panel, "2026-11-24 12:00:00", "2026-11-24 13:02:00");
  await expectOccupancy(panel, "2026-11-24 15:52:00", "—");
  await page.getByRole("navigation", { name: "Worker 页签" }).getByRole("button", { name: "Yellow" }).click();
  await expect(panel).toHaveCount(0);
  await expectInline(page, table.getByRole("button", { name: "192.0.2.2:22 设备组 0–1" }), cluster, "192.0.2.2:22 · 设备组 0–1");
  await panel.getByRole("button", { name: "关闭" }).click();
  await expect(panel).toHaveCount(0);
});
