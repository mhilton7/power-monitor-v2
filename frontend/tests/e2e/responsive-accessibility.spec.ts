import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Locator, type Page } from '@playwright/test';
import { mockApi } from './mocks';

const viewports = [
  { width: 320, height: 568 },
  { width: 375, height: 667 },
  { width: 390, height: 844 },
  { width: 768, height: 1024 },
  { width: 1024, height: 768 },
  { width: 1280, height: 720 },
  { width: 1440, height: 900 },
  { width: 1920, height: 1080 },
] as const;

async function expectNoHorizontalOverflow(page: Page) {
  const dimensions = await page.evaluate(() => ({ client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.client + 1);
}

async function expectUniqueIds(page: Page) {
  const duplicates = await page.locator('[id]').evaluateAll((nodes) => {
    const counts = new Map<string, number>();
    for (const node of nodes) counts.set(node.id, (counts.get(node.id) ?? 0) + 1);
    return [...counts.entries()].filter(([, count]) => count > 1);
  });
  expect(duplicates).toEqual([]);
}

async function expectTicksDoNotOverlap(ticks: Locator) {
  expect(await ticks.count()).toBeGreaterThanOrEqual(2);
  const boxes = await ticks.evaluateAll((nodes) => nodes.map((node) => node.getBoundingClientRect()).filter((box) => box.width > 0));
  for (let index = 1; index < boxes.length; index += 1) expect(boxes[index]!.left).toBeGreaterThanOrEqual(boxes[index - 1]!.right - 1);
}

for (const viewport of viewports) {
  test(`${viewport.width}x${viewport.height} keeps every major page contained and charts legible`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockApi(page);
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Live Power Usage' })).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await expectUniqueIds(page);
    await expectTicksDoNotOverlap(page.locator('[data-testid="usage-chart"] .recharts-xAxis-tick-labels text'));
    await expectTicksDoNotOverlap(page.locator('[data-testid="daily-chart"] .recharts-xAxis-tick-labels text'));
    const brushHandles = page.locator('[data-testid="usage-chart"] .recharts-brush-traveller');
    await expect(brushHandles).toHaveCount(2);
    for (const handle of await brushHandles.all()) {
      const box = await handle.boundingBox();
      expect(box?.width).toBeGreaterThanOrEqual(24);
      expect(box?.height).toBeGreaterThanOrEqual(24);
    }
    if (viewport.width <= 720) await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();

    await page.goto('/history');
    await expect(page.getByTestId('history-chart')).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await expectUniqueIds(page);
    await expectTicksDoNotOverlap(page.locator('[data-testid="history-chart"] .recharts-xAxis-tick-labels text'));

    await page.goto('/billing');
    await expect(page.getByRole('heading', { name: 'Billing' })).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await expectUniqueIds(page);

    await page.goto('/settings');
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await expectUniqueIds(page);
  });
}

test('320px active-home selector and dialogs remain within the viewport', async ({ page }) => {
  const firstHomeId = '00000000-0000-0000-0000-000000000010';
  const secondHomeId = '00000000-0000-0000-0000-000000000011';
  await page.setViewportSize({ width: 320, height: 568 });
  await mockApi(page, { homeScopesOverride: [{ id: firstHomeId, name: 'Duplicate home' }, { id: secondHomeId, name: 'Duplicate home' }] });
  await page.goto('/');
  const selector = page.getByLabel('Active home');
  await expect(selector.getByRole('option', { name: 'Duplicate home (1)' })).toHaveAttribute('value', firstHomeId);
  await expect(selector.getByRole('option', { name: 'Duplicate home (2)' })).toHaveAttribute('value', secondHomeId);
  await expectNoHorizontalOverflow(page);
  const selectorBox = await selector.boundingBox();
  expect(selectorBox?.x).toBeGreaterThanOrEqual(0);
  expect((selectorBox?.x ?? 0) + (selectorBox?.width ?? 0)).toBeLessThanOrEqual(320);

  await selector.selectOption(firstHomeId);
  await page.getByRole('button', { name: /Main Panel Sensor/ }).click();
  const dialog = page.getByRole('dialog', { name: 'Main Panel Sensor' });
  const dialogBox = await dialog.boundingBox();
  expect(dialogBox?.x).toBeGreaterThanOrEqual(0);
  expect((dialogBox?.x ?? 0) + (dialogBox?.width ?? 0)).toBeLessThanOrEqual(320);
  await expect(page.getByRole('button', { name: 'Close Main Panel Sensor' })).toBeVisible();
  await page.getByRole('button', { name: 'Close Main Panel Sensor' }).click();
  await expect(dialog).not.toBeVisible();

  await page.getByRole('button', { name: /active alerts across all authorized homes/ }).click();
  const alertDialog = page.getByRole('dialog', { name: 'Alerts & notifications' });
  const alertDialogBox = await alertDialog.boundingBox();
  expect(alertDialogBox?.x).toBeGreaterThanOrEqual(0);
  expect((alertDialogBox?.x ?? 0) + (alertDialogBox?.width ?? 0)).toBeLessThanOrEqual(320);
  const alertContentBoxes = await alertDialog.locator('.alert-content').evaluateAll((nodes) => nodes.map((node) => node.getBoundingClientRect().width));
  expect(alertContentBoxes.length).toBeGreaterThan(0);
  for (const width of alertContentBoxes) expect(width).toBeGreaterThanOrEqual(150);
  await expect(page.getByRole('button', { name: 'Close Alerts & notifications' })).toBeVisible();
  await page.getByRole('button', { name: 'Close Alerts & notifications' }).click();
  await expect(alertDialog).not.toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test('all major pages and a mobile modal pass automated WCAG checks', async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await mockApi(page);
  for (const path of ['/', '/history', '/billing', '/settings']) {
    await page.goto(path);
    await expect(page.locator('#main-content')).not.toBeEmpty();
    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze();
    expect(results.violations, `${path} accessibility violations`).toEqual([]);
  }

  await page.setViewportSize({ width: 320, height: 568 });
  await page.goto('/billing');
  await page.getByRole('button', { name: 'Import rates from SCE bill PDF' }).click();
  const modalResults = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze();
  expect(modalResults.violations).toEqual([]);
});
