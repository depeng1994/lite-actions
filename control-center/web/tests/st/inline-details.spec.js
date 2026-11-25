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
    const panel = await expectInline(
      page,
      page.locator(".occupancy-marker.occupancy-unknown").and(page.getByRole("button", { name: /192\.0\.2\.1:22/ })),
      pools,
      group,
    );
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
  const workerTabs = page.getByRole("navigation", { name: "Worker 页签" });
  const blueTab = workerTabs.getByRole("button", { name: "Blue" });
  const yellowTab = workerTabs.getByRole("button", { name: "Yellow" });
  await expect(blueTab.locator(".worker-commit")).toHaveText("5905edf");
  await expect(blueTab.locator(".worker-commit")).toHaveAttribute("title", "当前运行 Commit: 5905edfdb103a75b4d02e9543e02a0f46ccf2c1b");
  await expect(yellowTab.locator(".worker-commit")).toHaveText("a1b2c3d");
  await expect(yellowTab.locator(".worker-commit")).toHaveAttribute("title", "当前运行 Commit: a1b2c3d4e5f60718293a4b5c6d7e8f9012345678");
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

async function expectPageDurationsRounded(page) {
  const invalid = await page.evaluate(() => {
    const text = document.body.innerText;
    const matches = [...text.matchAll(/(?:已用|已用时长|耗时)[^\d\n-]*(-?\d+(?:\.\d+)?)\s*分钟/g)];
    return matches
      .map((match) => match[1])
      .filter((num) => Number(num) < 0 || /\.\d{3,}$/.test(num));
  });
  expect(invalid).toEqual([]);
}

test("all elapsed durations across Pools, Models, and detail dialogs round to at most 2 decimal places even after long uptime", async ({ page }) => {
  // Load 76 minutes before backend `now` so module-level clock stays 75+ minutes behind live tasks.
  await page.clock.setFixedTime(new Date("2026-11-24T06:44:00Z"));
  const login = await page.request.post("/api/v1/auth/login", {
    data: { username: "super", secret: "browser-fixture-super-secret" },
  });
  expect(login.ok()).toBe(true);
  await page.goto("/#/models");
  await expect(page.getByRole("row", { name: /Workflow #101/ })).toBeVisible();

  await page.clock.setFixedTime(new Date("2026-11-24T08:00:00Z"));

  // ModelsView: expand active (#101, #103) and historical (#102) workflows and check table + modal durations.
  await page.getByRole("button", { name: "展开 Workflow #101 的子用例" }).click();
  await page.getByRole("button", { name: "展开 Workflow #102 的子用例" }).click();
  await page.getByRole("button", { name: "展开 Workflow #103 的子用例" }).click();

  const row101 = page.getByRole("row", { name: "Workflow #101 · 尝试 1", exact: true });
  const row103 = page.getByRole("row", { name: "Workflow #103 · 尝试 1", exact: true });
  await expect(row101).toContainText("0.02");
  await expect(row103).toContainText("5.02");

  const shortActiveCell = page.locator("tr", { hasText: "零碎秒活跃任务" }).locator("td").last();
  const longActiveCell = page.locator("tr", { hasText: "长时零碎秒任务" }).locator("td").last();
  const shortHistoryCell = page.locator("tr", { hasText: "零碎秒归档任务" }).locator("td").last();
  await expect(shortActiveCell).toHaveText(/已用 0\.67 分钟/);
  await expect(longActiveCell).toHaveText(/已用 150\.67 分钟/);
  await expect(shortHistoryCell).toHaveText("0.67 分钟");
  await expectPageDurationsRounded(page);

  await page.getByRole("button", { name: "零碎秒活跃任务" }).click();
  const modal = page.locator("dialog[open]");
  await expect(modal.getByText("已用时长", { exact: true }).locator("..").locator("dd")).toHaveText("0.67 分钟");
  await expectPageDurationsRounded(page);
  await modal.getByRole("button", { name: "关闭" }).click();

  await page.getByRole("button", { name: "零碎秒归档任务" }).click();
  await expect(modal.getByText("耗时", { exact: true }).locator("..").locator("dd")).toHaveText("0.67 分钟");
  await expectPageDurationsRounded(page);
  await modal.getByRole("button", { name: "关闭" }).click();

  // PoolsView: check timeline bar `.lease-meta` and inline CaseDialog / HistoryDialog durations.
  await page.goto("/#/pools");
  const longBar = page.locator(".lease").and(page.getByRole("button", { name: /长时零碎秒任务/ }));
  await expect(longBar.locator(".lease-meta")).toHaveText(/运行中 · 已用 150\.67 分钟/);
  await expectPageDurationsRounded(page);

  const inlinePanel = await expectInline(page, longBar, pools, "长时零碎秒任务");
  await expect(inlinePanel.getByText("已用时长", { exact: true }).locator("..").locator("dd")).toHaveText("150.67 分钟");

  const shortBar = page.locator(".lease").and(page.getByRole("button", { name: /零碎秒活跃任务/ }));
  await expectInline(page, shortBar, pools, "零碎秒活跃任务");
  await expect(inlinePanel.getByText("已用时长", { exact: true }).locator("..").locator("dd")).toHaveText("0.67 分钟");

  const shortHistBar = page.locator(".lease").and(page.getByRole("button", { name: /零碎秒归档任务/ }));
  await expectInline(page, shortHistBar, pools, "零碎秒归档任务");
  await expect(inlinePanel.getByText("耗时", { exact: true }).locator("..").locator("dd")).toHaveText("0.67 分钟");

  const intActiveBar = page.locator(".lease").and(page.getByRole("button", { name: /运行用例/ }));
  await expectInline(page, intActiveBar, pools, "运行用例");
  await expect(inlinePanel.getByText("已用时长", { exact: true }).locator("..").locator("dd")).toHaveText("35 分钟");

  const intHistBar = page.locator(".lease").and(page.getByRole("button", { name: /历史用例/ })).first();
  await expectInline(page, intHistBar, pools, "历史用例");
  await expect(inlinePanel.getByText("耗时", { exact: true }).locator("..").locator("dd")).toHaveText("30 分钟");
  await expectPageDurationsRounded(page);
});

test("completed TEST tasks stay on Pools timeline and multi-node allocations render every host in Models and details", async ({ page }) => {
  await setup(page);
  await page.goto("/#/pools");

  // Completed TEST single-node and multi-node cases keep their historical bars on #/pools.
  const singleTestBar = page.locator(".lease").and(page.getByRole("button", { name: /已完成测试单卡用例/ }));
  await expect(singleTestBar).toHaveCount(1);
  const multiTestBars = page.locator(".lease").and(page.getByRole("button", { name: /已完成测试双机用例/ }));
  await expect(multiTestBars).toHaveCount(2);
  await expect(multiTestBars.first()).toHaveAccessibleName(/192\.0\.2\.1:22 设备组 14–15、192\.0\.2\.4:22 设备组 0–1/);

  const inlinePanel = await expectInline(page, multiTestBars.first(), pools, "已完成测试双机用例");
  await expect(inlinePanel.getByText("实际分配", { exact: true }).locator("..").locator("dd"))
    .toHaveText("Blue / blue_pool · 192.0.2.1:22、192.0.2.4:22");
  await expect(inlinePanel.getByText("设备 ID", { exact: true }).locator("..").locator("dd"))
    .toHaveText("192.0.2.1:22: 14,15 / 192.0.2.4:22: 0,1");

  // #/models shows completed TEST workflow with both hosts paired with their own devices.
  await page.goto("/#/models");
  await page.getByRole("button", { name: "展开 Workflow #104 的子用例" }).click();
  const singleRow = page.locator(".cases-table tbody tr", { hasText: "已完成测试单卡用例" });
  await expect(singleRow.locator("td").nth(3)).toContainText("192.0.2.1:22");
  await expect(singleRow.locator("td").nth(3)).toContainText("8–9");

  const multiRow = page.locator(".cases-table tbody tr", { hasText: "已完成测试双机用例" });
  await expect(multiRow.locator("td").nth(3)).toHaveText("192.0.2.1:22 14–15、192.0.2.4:22 0–1");

  await page.getByRole("button", { name: "已完成测试双机用例" }).click();
  const modal = page.locator("dialog[open]");
  await expect(modal.getByText("实际分配", { exact: true }).locator("..").locator("dd"))
    .toHaveText("Blue / blue_pool · 192.0.2.1:22、192.0.2.4:22");
  await expect(modal.getByText("设备 ID", { exact: true }).locator("..").locator("dd"))
    .toHaveText("192.0.2.1:22: 14,15 / 192.0.2.4:22: 0,1");
  await modal.getByRole("button", { name: "关闭" }).click();
});
