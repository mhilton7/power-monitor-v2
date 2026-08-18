import { expect, test } from '@playwright/test';
import { mockApi } from './mocks';

test.beforeEach(async ({ page }) => { await mockApi(page); });

test('History renders saved gaps, recovered energy, cost tooltips and non-overlapping time ticks', async ({ page }) => {
  await page.goto('/history');
  await expect(page.getByTestId('history-chart')).toBeVisible();
  await expect(page.getByText(/Showing readings for Main service/)).toBeVisible();
  await expect(page.getByText(/measured zero renders at zero/)).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Connection gap details' })).toBeVisible();
  await expect(page.getByText(/0.42 kWh recovered/)).toBeVisible();
  const ticks = page.locator('[data-testid="history-chart"] .recharts-xAxis-tick-labels text');
  await expect(ticks).not.toHaveCount(0);
  expect(await ticks.count()).toBeGreaterThanOrEqual(2);
  const boxes = await ticks.evaluateAll((nodes) => nodes.map((node) => node.getBoundingClientRect()).filter((box) => box.width > 0));
  for (let index = 1; index < boxes.length; index += 1) {
    expect(boxes[index]!.left).toBeGreaterThanOrEqual(boxes[index - 1]!.right - 1);
  }
  const areaPath = await page.locator('[data-testid="history-chart"] .recharts-area-area').getAttribute('d');
  expect(areaPath?.match(/M/g)?.length).toBeGreaterThanOrEqual(2);
  await page.getByTestId('history-chart').locator('.recharts-wrapper').hover({ position: { x: 150, y: 180 }, force: true });
  await expect(page.getByText(/Estimated cost:/)).toBeVisible();
});

test('mobile History chart remains gap-aware with readable labels', async ({ page }) => {
  await page.setViewportSize({ width: 412, height: 915 });
  await page.goto('/history');
  await expect(page.getByTestId('history-chart')).toBeVisible();
  await expect(page).toHaveScreenshot('history-mobile-412x915.png', { fullPage: false });
});
