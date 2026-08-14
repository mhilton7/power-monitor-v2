import { expect, test } from '@playwright/test';
import { mockApi } from './mocks';

test.beforeEach(async ({ page }) => { await mockApi(page); });

test('History renders committed gaps, cost tooltips and non-overlapping time ticks', async ({ page }) => {
  await page.goto('/history');
  await expect(page.getByTestId('history-chart')).toBeVisible();
  await expect(page.getByText('Authenticated sensor evidence unavailable')).toBeVisible();
  await expect(page.getByText(/measured zero renders at zero/)).toBeVisible();
  const ticks = page.locator('[data-testid="history-chart"] .recharts-xAxis .recharts-cartesian-axis-tick-value');
  const boxes = await ticks.evaluateAll((nodes) => nodes.map((node) => node.getBoundingClientRect()).filter((box) => box.width > 0));
  for (let index = 1; index < boxes.length; index += 1) {
    expect(boxes[index]!.left).toBeGreaterThanOrEqual(boxes[index - 1]!.right - 1);
  }
  await page.getByTestId('history-chart').locator('.recharts-wrapper').hover({ position: { x: 150, y: 180 }, force: true });
  await expect(page.getByText(/Estimated cost:/)).toBeVisible();
});

test('mobile History chart remains gap-aware with readable labels', async ({ page }) => {
  await page.setViewportSize({ width: 412, height: 915 });
  await page.goto('/history');
  await expect(page.getByTestId('history-chart')).toBeVisible();
  await expect(page).toHaveScreenshot('history-mobile-412x915.png', { fullPage: false });
});
