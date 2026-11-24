import { expect, test } from "@playwright/test";

async function setup(page) {
  await page.clock.setFixedTime(new Date("2026-11-24T08:00:00Z"));
  const login = await page.request.post("/api/v1/auth/login", {
    data: { username: "super", secret: "browser-fixture-super-secret" },
  });
  expect(login.ok()).toBe(true);
}

const pools = { panel: "#timeline-detail", above: ".timeline-board", below: ".queue-section" };
const cluster = { panel: "#cluster-detail", above: ".cluster-table", below: '[aria-label="历史配置版本"]' };

async function expectInline(page, source, placement, title) {
  await source.click();
  const panel = page.locator(placement.panel);
  await expect(panel.getByRole("heading", { name: title, exact: true })).toBeVisible();
  await expect(page.locator("dialog[open]")).toHaveCount(0);
  expect(await panel.evaluate((element, { above, below, panel }) => {
    const frame = element.getBoundingClientRect();
    return frame.top >= document.querySelector(above).getBoundingClientRect().bottom
      && frame.bottom <= document.querySelector(below).getBoundingClientRect().top - (panel === "#timeline-detail" ? 15 : 0);
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
      await expect(bar).toHaveCount(label === "历史用例" ? 2 : 1);
      await expectTaskTimes(await expectInline(page, bar.first(), pools, label), start, end);
    });
  }
  await test.step("排队表中的用例", async () => {
    const label = "等待外部释放用例";
    const entry = page.locator(".queue-table").getByRole("button", { name: label });
    await expectTaskTimes(await expectInline(page, entry, pools, label), "—", "—");
  });
  await test.step("外部占用区间", async () => {
    const [completed, active] = await page.locator(".occupancy-history").all();
    const closedPanel = await expectInline(page, completed, pools, group);
    await expectOccupancy(closedPanel, "2026-11-24 12:00:00", "2026-11-24 12:02:00");
    await expect(closedPanel.getByRole("row")).toHaveCount(2);
    const openPanel = await expectInline(page, active, pools, group);
    await expectOccupancy(openPanel, "2026-11-24 15:52:00", "—");
  });
  await test.step("当前状态标记和资源名称", async () => {
    const panel = await expectInline(page, page.locator(".occupancy-marker.occupancy-unknown"), pools, group);
    await expectOccupancy(panel, "2026-11-24 12:00:00", "2026-11-24 12:02:00");
    await expectOccupancy(panel, "2026-11-24 15:52:00", "—");
    const idlePanel = await expectInline(page, page.locator(".occupancy-marker.occupancy-idle").and(page.getByRole("button", { name: /192\.0\.2\.2:22/ })),
      pools, "192.0.2.2:22 · 设备组 0–1");
    await expect(idlePanel.getByText("暂无外部占用记录")).toBeVisible();
    await expectInline(page, page.locator(".tree-leaf").filter({ hasText: /192\.0\.2\.1:22.*0–1/ }), pools, group);
    await panel.getByRole("button", { name: "关闭" }).click();
    await expect(panel).toHaveCount(0);
  });
});

test("cluster reuses the detail slot checks for Pool, node and device groups", async ({ page }) => {
  await setup(page);
  await page.goto("/#/cluster");
  const table = page.locator(".cluster-table");
  const poolPanel = await expectInline(page, table.getByRole("button", { name: "Pool blue_pool" }), cluster, "Blue / blue_pool");
  await expectOccupancy(poolPanel, "2026-11-24 15:52:00", "—");
  const nodePanel = await expectInline(page, table.getByRole("button", { name: "192.0.2.3:22", exact: true }), cluster, "192.0.2.3:22");
  await expect(nodePanel.getByText("暂无外部占用记录")).toBeVisible();
  const group = table.getByRole("button", { name: "192.0.2.1:22 设备组 0–1" });
  const panel = await expectInline(page, group, cluster, "192.0.2.1:22 · 设备组 0–1");
  await expectOccupancy(panel, "2026-11-24 12:00:00", "2026-11-24 12:02:00");
  await expectOccupancy(panel, "2026-11-24 15:52:00", "—");
  await page.getByRole("navigation", { name: "Worker 页签" }).getByRole("button", { name: "Yellow" }).click();
  await expect(panel).toHaveCount(0);
  await expectInline(page, table.getByRole("button", { name: "192.0.2.2:22 设备组 0–1" }), cluster, "192.0.2.2:22 · 设备组 0–1");
  await panel.getByRole("button", { name: "关闭" }).click();
  await expect(panel).toHaveCount(0);
});
