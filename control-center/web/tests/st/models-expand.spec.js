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
