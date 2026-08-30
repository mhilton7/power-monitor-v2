import { expect, test } from '@playwright/test';
import { mockApi } from './mocks';

test.beforeEach(async ({ page }) => { await mockApi(page); });

test('History renders saved gaps, recovered energy, cost tooltips and non-overlapping time ticks', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-08-13T23:30:00Z'));
  await page.goto('/history');
  await expect(page.getByTestId('history-chart')).toBeVisible();
  await expect(page.getByText(/Showing readings for Main service/)).toBeVisible();
  await expect(page.getByText(/measured zero renders at zero/)).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Connection gap details' })).toBeVisible();
  await expect(page.getByText(/0.42 kWh recovered/)).toBeVisible();
  await expect(page.locator('.history-summary-grid').first()).toContainText('PDT');
  await expect(page.getByRole('heading', { name: 'Active power over time' }).locator('..')).toContainText('display America/Los_Angeles');
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

test('History keyboard range remains manual until Reset zoom or Resume live', async ({ page }) => {
  await page.goto('/history');
  const chart = page.getByTestId('history-chart');
  const selectedRange = page.getByTestId('history-selected-range');
  await expect(chart).toBeVisible();
  await page.getByRole('button', { name: '24 hours' }).click();
  const initialStart = Number(await selectedRange.getAttribute('data-start-ms'));
  const initialEnd = Number(await selectedRange.getAttribute('data-end-ms'));

  await page.getByTestId('history-range-start').press('ArrowRight');
  await page.getByTestId('history-range-end').press('ArrowLeft');
  await expect(chart).toHaveAttribute('data-range-mode', 'manual');
  const manualStart = Number(await selectedRange.getAttribute('data-start-ms'));
  const manualEnd = Number(await selectedRange.getAttribute('data-end-ms'));
  expect(manualStart).toBeGreaterThan(initialStart);
  expect(manualEnd).toBeLessThan(initialEnd);

  await page.getByRole('button', { name: '24 hours' }).click();
  await expect(selectedRange).toHaveAttribute('data-start-ms', String(manualStart));
  await expect(selectedRange).toHaveAttribute('data-end-ms', String(manualEnd));

  await page.getByRole('button', { name: 'Reset zoom' }).click();
  await expect(chart).toHaveAttribute('data-range-mode', 'auto');
  await expect(selectedRange).toHaveAttribute('data-start-ms', String(initialStart));
  await expect(selectedRange).toHaveAttribute('data-end-ms', String(initialEnd));

  await page.getByRole('button', { name: 'Live' }).click();
  await page.getByTestId('history-range-start').press('ArrowRight');
  await expect(chart).toHaveAttribute('data-range-mode', 'manual');
  await page.getByRole('button', { name: 'Resume live' }).click();
  await expect(chart).toHaveAttribute('data-range-mode', 'auto');
  await expect(page.getByRole('button', { name: 'Resume live' })).toHaveCount(0);
});

test('mobile History chart remains gap-aware with readable labels', async ({ page }) => {
  await page.setViewportSize({ width: 412, height: 915 });
  await page.goto('/history');
  await expect(page.getByTestId('history-chart')).toBeVisible();
  await expect(page).toHaveScreenshot('history-mobile-412x915.png', { fullPage: false });
});
