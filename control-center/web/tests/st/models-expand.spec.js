import { expect, test } from "@playwright/test";

async function openModels(page, viewport) {
  await page.setViewportSize(viewport);
  await page.clock.setFixedTime(new Date("2026-11-24T08:00:00Z"));
  await page.goto("/#/models");
  await expect(page.getByText(/已显示 5 \//)).toBeVisible();
}

async function expectRowVisibleInViewport(locator) {
  await expect(locator).toBeVisible();
  const bounds = await locator.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return {
      top: rect.top,
      bottom: rect.bottom,
      viewportHeight: Math.min(window.innerHeight, document.documentElement.clientHeight || window.innerHeight),
    };
  });
  expect(bounds.top).toBeGreaterThanOrEqual(0);
  expect(bounds.bottom).toBeLessThanOrEqual(bounds.viewportHeight);
}

test("ModelsView fills a wide window and keeps the Actions link on its first line", async ({ page }) => {
  await openModels(page, { width: 2080, height: 900 });
  const board = page.getByRole("main");
  const table = page.locator(".workflow-table-scroll");
  const row = page.getByRole("row", { name: "Workflow #100 · 尝试 1", exact: true });
  const actions = row.locator("td").nth(5);
  const link = actions.getByRole("link", { name: "Smoke #98 · 尝试 1", exact: true });
  await expect(link).toHaveAttribute("href", /\/actions\/runs\/100$/);
  await expect(actions.locator("small")).toHaveText("Lite Actions PR smoke: #7");
  expect(await board.evaluate((main) => main.clientWidth)).toBeGreaterThan(2000);
  expect(await table.evaluate((wrapper) => wrapper.clientWidth)).toBeGreaterThan(1950);
  for (const width of [2080, 736]) {
    await page.setViewportSize({ width, height: 900 });
    const geometry = await actions.evaluate((cell) => {
      const first = cell.querySelector("a");
      const second = cell.querySelector("small");
      const style = getComputedStyle(first);
      return {
        top: first.getBoundingClientRect().bottom,
        bottom: second.getBoundingClientRect().top,
        weight: Number(style.fontWeight),
        accent: style.color === getComputedStyle(document.querySelector(".text-button")).color,
        primaryHeight: first.getBoundingClientRect().height,
        lineHeight: parseFloat(style.lineHeight),
      };
    });
    expect(geometry.top).toBeLessThanOrEqual(geometry.bottom);
    expect(geometry.weight).toBeGreaterThanOrEqual(600);
    expect(geometry.accent).toBe(true);
    expect(geometry.primaryHeight).toBeLessThanOrEqual(geometry.lineHeight + 1);
  }
  expect(await table.evaluate((wrapper) => wrapper.scrollWidth > wrapper.clientWidth)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

for (const viewport of [
  { width: 1280, height: 800 },
  { width: 1280, height: 900 },
]) {
  test(`ModelsView reveals expanded bottom workflow rows without manual scrolling at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    await openModels(page, viewport);

    const scrollTrap = await page.locator(".workflow-table-scroll").evaluate((element) => ({
      scrollHeight: element.scrollHeight,
      clientHeight: element.clientHeight,
    }));
    expect(scrollTrap.scrollHeight).toBeLessThanOrEqual(scrollTrap.clientHeight + 1);

    await page.getByRole("button", { name: "展开 Workflow #100 的子用例" }).click();

    const parent100 = page.getByRole("row", { name: "Workflow #100 · 尝试 1", exact: true });
    const subtable100 = page.getByRole("table", { name: "Workflow #100 的子用例" });
    const lastChild100 = subtable100.locator("tbody tr").last();

    await expectRowVisibleInViewport(parent100);
    await expectRowVisibleInViewport(subtable100.locator("thead tr"));
    await expectRowVisibleInViewport(lastChild100);

    const fit = await page.evaluate(() => {
      const sc = document.querySelector(".workflow-table-scroll");
      const scRect = sc.getBoundingClientRect();
      const ctRect = document.querySelector(".cases-table").getBoundingClientRect();
      return {
        scrollHeight: sc.scrollHeight,
        clientHeight: sc.clientHeight,
        withinLeft: ctRect.left >= scRect.left,
        withinRight: ctRect.right <= scRect.right,
        withinBottom: ctRect.bottom <= scRect.bottom,
      };
    });
    expect(fit.scrollHeight).toBeLessThanOrEqual(fit.clientHeight + 1);
    expect(fit.withinLeft).toBe(true);
    expect(fit.withinRight).toBe(true);
    expect(fit.withinBottom).toBe(true);

    const scrollBeforeCollapse = await page.evaluate(() => window.scrollY);
    await page.getByRole("button", { name: "收起 Workflow #100 的子用例" }).click();
    await expect(subtable100).toHaveCount(0);
    const scrollAfterCollapse = await page.evaluate(() => window.scrollY);
    expect(scrollAfterCollapse).toBeLessThanOrEqual(scrollBeforeCollapse);

    await page.getByRole("button", { name: "展开 Workflow #101 的子用例" }).click();
    const parent101 = page.getByRole("row", { name: "Workflow #101 · 尝试 1", exact: true });
    const child101 = page.getByRole("table", { name: "Workflow #101 的子用例" }).locator("tbody tr").last();
    await expectRowVisibleInViewport(parent101);
    await expectRowVisibleInViewport(child101);
  });
}

test("ModelsView keeps parent and child cases visible when expanding in a tall table or short viewport", async ({ page }) => {
  await openModels(page, { width: 1280, height: 800 });

  await page.getByRole("button", { name: "展开 Workflow #102 的子用例" }).click();
  await page.getByRole("button", { name: "展开 Workflow #103 的子用例" }).click();
  await page.getByRole("button", { name: "展开 Workflow #101 的子用例" }).click();

  await expectRowVisibleInViewport(page.getByRole("row", { name: "Workflow #101 · 尝试 1", exact: true }));
  await expectRowVisibleInViewport(page.getByRole("table", { name: "Workflow #101 的子用例" }).locator("tbody tr").last());

  await page.setViewportSize({ width: 1280, height: 420 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.getByRole("button", { name: "展开 Workflow #100 的子用例" }).click();

  const parent100 = page.getByRole("row", { name: "Workflow #100 · 尝试 1", exact: true });
  const firstChild100 = page.getByRole("table", { name: "Workflow #100 的子用例" }).locator("tbody tr").first();
  await expectRowVisibleInViewport(parent100);
  await expectRowVisibleInViewport(firstChild100);
});

test("ModelsView displays 运行中 for in-flight or partially completed active workflows and 排队中 only when all cases are queued", async ({ page }) => {
  await openModels(page, { width: 1280, height: 900 });

  const expectWorkflowStatus = async (runId, label, toneClass) => {
    const row = page.getByRole("row", { name: `Workflow #${runId} · 尝试 1`, exact: true });
    await expect(row).toBeVisible();
    const badge = row.locator("td").nth(6).locator(".tag");
    await expect(badge).toHaveText(label);
    await expect(badge).toHaveClass(new RegExp(`\\b${toneClass}\\b`));
  };

  // Mixed queued + running + staging + cleaning + completed -> 运行中
  await expectWorkflowStatus("100", "运行中", "tag-teal");
  // Single running case -> 运行中
  await expectWorkflowStatus("101", "运行中", "tag-teal");
  // All cases queued -> 排队中
  await expectWorkflowStatus("105", "排队中", "tag-amber");
  // Completed case + queued case (already started workflow) -> 运行中
  await expectWorkflowStatus("106", "运行中", "tag-teal");
  // Multi-zone merged TEST workflow (Blue running + Yellow queued) -> 运行中
  await expectWorkflowStatus("107", "运行中", "tag-teal");
  // Terminal history workflows keep terminal statuses
  await expectWorkflowStatus("102", "成功", "tag-teal");
  await expectWorkflowStatus("104", "成功", "tag-teal");

  // Status filter "仅活动" keeps both 运行中 and 排队中 active rows while hiding history.
  await page.getByLabel("状态").selectOption("active");
  await expect(page.getByRole("row", { name: "Workflow #100 · 尝试 1", exact: true })).toBeVisible();
  await expect(page.getByRole("row", { name: "Workflow #105 · 尝试 1", exact: true })).toBeVisible();
  await expect(page.getByRole("row", { name: "Workflow #106 · 尝试 1", exact: true })).toBeVisible();
  await expect(page.getByRole("row", { name: "Workflow #107 · 尝试 1", exact: true })).toBeVisible();
  await expect(page.getByRole("row", { name: "Workflow #102 · 尝试 1", exact: true })).toHaveCount(0);

  // Column filter "调度结果" filters Running vs Queued accurately.
  await page.getByRole("button", { name: /列筛选/ }).click();
  await page.getByLabel("筛选调度结果").fill("运行中");
  await expect(page.getByRole("row", { name: "Workflow #100 · 尝试 1", exact: true })).toBeVisible();
  await expect(page.getByRole("row", { name: "Workflow #106 · 尝试 1", exact: true })).toBeVisible();
  await expect(page.getByRole("row", { name: "Workflow #107 · 尝试 1", exact: true })).toBeVisible();
  await expect(page.getByRole("row", { name: "Workflow #105 · 尝试 1", exact: true })).toHaveCount(0);
});

test("ModelsView loads filtered GitCode test workflows beyond the global 5-row page and refreshes newly finished runs into history", async ({ page }) => {
  await openModels(page, { width: 1280, height: 900 });

  // Initially the 5 newest global history runs are GitHub runs, pushing the older GitCode test run off page 1.
  const gcRunId = "57d83e103f694111be5cf35420780955";
  await expect(page.getByRole("row", { name: `Workflow #${gcRunId} · 尝试 1`, exact: true })).toHaveCount(0);

  // Filtering by platform=GitCode queries the backend instead of returning 0/0 from the 5-row global slice.
  await page.locator(".workflow-filters").getByLabel("平台").selectOption("GitCode");
  const gcRow = page.getByRole("row", { name: `Workflow #${gcRunId} · 尝试 1`, exact: true });
  await expect(gcRow).toBeVisible();
  await expect(page.getByText("已显示 1 / 1 个历史 Workflow")).toBeVisible();
  await expect(gcRow.locator("td").nth(3)).toHaveText("Blue / blue_pool");
  await expect(gcRow.locator("td").nth(5)).toContainText("TEST");
  await expect(gcRow.locator("td").nth(5).getByRole("link")).toHaveAttribute(
    "href",
    `https://gitcode.com/depeng1994/torchtitan-npu/actions/runs/${gcRunId}`,
  );
  await expect(gcRow.locator("td").nth(6).locator(".tag")).toHaveText("失败");
  await expect(gcRow.locator("td").nth(8)).toHaveText("手动");

  // Expanding shows child cases cleanly.
  await page.getByRole("button", { name: `展开 Workflow #${gcRunId} 的子用例` }).click();
  await expect(page.getByRole("table", { name: `Workflow #${gcRunId} 的子用例` })).toContainText("GitCode 16P smoke");

  // Simulate an active GitCode test workflow finishing while #/models remains open:
  // on the next 30-second auto-refresh it must move into history instead of vanishing.
  let transitioned = false;
  const liveGcId = "794904a3d9dc497d9597c432fb6eed2c";
  const makeGcRun = (active) => ({
    provider: "gitcode",
    repo: "depeng1994/torchtitan-npu",
    run_id: liveGcId,
    attempt: 1,
    is_test: true,
    reporter_ids: ["blue-test-id109a1"],
    sequence: active ? 0 : -1,
    submitted_at: Math.floor(Date.parse("2026-11-24T07:50:00Z") / 1000),
    run_number: 37,
    display_title: null,
    repo_scope: "个人仓",
    logical_model: "torchtitan-npu",
    workflow_name: "lite-ci-dispatcher-smoke",
    workflow_file: ".gitcode/workflows/dispatcher_smoke.yml",
    pools: ["blue_pool"],
    max_cards: 8,
    result: active ? "RUNNING" : "FAIL",
    trigger_actor: "depeng1994",
    event_type: "Manual",
    pr_number: null,
    pr_author: null,
    started_at: Math.floor(Date.parse("2026-11-24T07:51:00Z") / 1000),
    finished_at: active ? null : Math.floor(Date.parse("2026-11-24T08:00:10Z") / 1000),
    card_hours: 0.4,
    card_hours_as_of_now: active,
    card_hours_partial: false,
    cancel_supported: false,
    can_move_up: false,
    cases: [{
      task_uid: "t_live_gc_case",
      worker_id: "blue",
      reporter_id: "blue-test-id109a1",
      is_test: true,
      provider: "gitcode",
      repo: "depeng1994/torchtitan-npu",
      run_id: liveGcId,
      attempt: 1,
      dispatch_id: null,
      run_number: 37,
      display_title: null,
      case_id: "case_002_dispatch_smoke_8p",
      label: "Live GitCode 8P smoke",
      kind: "npu_smoke",
      pr_number: null,
      pr_author: null,
      trigger_actor: "depeng1994",
      event_type: "Manual",
      workflow_file: ".gitcode/workflows/dispatcher_smoke.yml",
      logical_model: "torchtitan-npu",
      test_id: "dispatch_smoke_8p",
      target_pool: "blue_pool",
      req_nodes: 1,
      req_devices: 8,
      phase: active ? "RUNNING" : null,
      status: active ? null : "FAIL",
      queued_at: Math.floor(Date.parse("2026-11-24T07:50:00Z") / 1000),
      finished_at: active ? null : Math.floor(Date.parse("2026-11-24T08:00:10Z") / 1000),
      queue_pos: null,
      duration_sec: active ? null : 180,
      submitted_at: Math.floor(Date.parse("2026-11-24T07:50:00Z") / 1000),
      workflow_name: "lite-ci-dispatcher-smoke",
      started_at: Math.floor(Date.parse("2026-11-24T07:51:00Z") / 1000),
      allocated_cards: 8,
      storage_tier: "hot",
      allocation: [],
      estimated_sec: null,
      sample_count: 0,
      estimate_status: "NO_ESTIMATE",
      can_move_up: false,
      cancel_supported: false,
      run_url: `https://gitcode.com/depeng1994/torchtitan-npu/actions/runs/${liveGcId}`,
    }],
  });

  await page.route("**/api/v1/public/workflows*", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    const url = new URL(route.request().url());
    const isHistory = url.searchParams.get("status") === "history";
    if (!transitioned) {
      if (!isHistory) payload.active = [...payload.active, makeGcRun(true)];
    } else if (isHistory && (!url.searchParams.get("platform") || url.searchParams.get("platform") === "gitcode")) {
      payload.history = [makeGcRun(false), ...payload.history];
      payload.history_total += 1;
    }
    await route.fulfill({ response, json: payload });
  });

  // Trigger a refresh tick so the live GitCode workflow enters as active.
  await page.clock.runFor(30_000);
  const liveRow = page.getByRole("row", { name: `Workflow #${liveGcId} · 尝试 1`, exact: true });
  await expect(liveRow).toBeVisible();
  await expect(liveRow.locator("td").nth(6).locator(".tag")).toHaveText("运行中");

  // Transition it from active -> finished and advance 30s; it must remain visible in history.
  transitioned = true;
  await page.clock.runFor(30_000);
  await expect(liveRow).toBeVisible();
  await expect(liveRow.locator("td").nth(6).locator(".tag")).toHaveText("失败");
  await expect(page.getByText("已显示 2 / 2 个历史 Workflow")).toBeVisible();
});
