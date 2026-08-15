import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';
import { home } from '../fixtures';
import { mockApi } from './mocks';

test.beforeEach(async ({ page }) => { await mockApi(page); });

test('Home shows live readings, committed summaries and sensor evidence', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('2.48', { exact: true })).toBeVisible();
  await expect(page.getByText('Live heartbeat measurement · not yet committed History')).toBeVisible();
  await expect(page.getByText('18.74 kWh')).toBeVisible();
  await expect(page.getByText('$0.43')).toBeVisible();
  await expect(page.getByTestId('usage-chart')).toBeVisible();
  const usagePath = await page.locator('[data-testid="usage-chart"] .recharts-area-area').getAttribute('d');
  expect(usagePath?.match(/M/g)?.length).toBeGreaterThanOrEqual(2);
  await expect(page.getByRole('button', { name: /Main Panel Sensor/ })).toBeVisible();
});

test('Home preserves measured zero while showing missing voltage as unavailable', async ({ page }) => {
  await page.unrouteAll({ behavior: 'wait' });
  await mockApi(page, { homeOverride: { ...home, devices: [{ ...home.devices[0]!, measurement: { ...home.devices[0]!.measurement, active_power_w: 0, voltage_v: null } }] } });
  await page.goto('/');
  await expect(page.getByLabel('0 kilowatts', { exact: true })).toBeVisible();
  const voltageCard = page.getByText('Voltage').locator('..').locator('..').locator('..');
  await expect(voltageCard.getByText('Not available')).toBeVisible();
});

test('sensor reboot, maintenance sleep and format use guarded command flows', async ({ page }) => {
  const commands = await mockApi(page);
  await page.goto('/');
  await page.getByRole('button', { name: /Main Panel Sensor/ }).click();
  await page.getByRole('button', { name: /Reboot/ }).last().click();
  await expect(page.getByText('Measurement pauses briefly')).toBeVisible();
  await page.getByRole('button', { name: 'Queue command' }).click();
  await expect.poll(() => commands).toContain('reboot');
  await page.getByRole('button', { name: /Maintenance sleep/ }).click();
  await expect(page.getByText(/does not disconnect mains power/)).toBeVisible();
  await page.getByRole('button', { name: 'Cancel' }).click();
  await page.getByRole('button', { name: /Format microSD history/ }).click();
  await page.getByRole('button', { name: 'Queue command' }).click();
  await expect(page.getByRole('heading', { name: 'Commit microSD history format?' })).toBeVisible();
  await page.getByLabel(/Type FORMAT STORAGE/).fill('FORMAT STORAGE');
  await page.getByRole('button', { name: 'Queue command' }).click();
  await expect.poll(() => commands).toEqual(expect.arrayContaining(['format_storage_prepare', 'format_storage_commit']));
});

test('unreported OTA state stays unavailable and commands can be forbidden by the server', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: /Main Panel Sensor/ }).click();
  await expect(page.getByText('Not reported').last()).toBeVisible();
  await expect(page.getByText(/release availability is not reported/i)).not.toBeVisible();
  await page.unrouteAll({ behavior: 'wait' });
  await mockApi(page, { forbiddenCommands: true });
  await page.reload();
  await page.getByRole('button', { name: /Main Panel Sensor/ }).click();
  await page.getByRole('button', { name: /Reboot/ }).last().click();
  await page.getByRole('button', { name: 'Queue command' }).click();
  await expect(page.getByText('The server refused this command.')).toBeVisible();
});

test('Home has no serious accessibility violations and supports keyboard navigation', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Live Power Usage' })).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.locator('#main-content')).toBeFocused();
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze();
  expect(results.violations).toEqual([]);
});

test('alert acknowledgement and silence retain evidence and call scoped routes', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: '1 active alerts across all authorized homes' }).click();
  const alertDialog = page.getByRole('dialog', { name: 'Alerts & notifications' });
  await expect(alertDialog.getByRole('heading', { name: 'Alerts & notifications' })).toBeVisible();
  await expect(alertDialog).toContainText('Alerts span all homes this account can access.');
  await expect(page.getByText('backlog', { exact: true })).toBeVisible();
  const acknowledgeRequest = page.waitForRequest((request) => request.method() === 'POST' && request.url().endsWith('/alerts/alert-backlog/acknowledge'));
  await page.getByRole('button', { name: 'Acknowledge reading backlog' }).click();
  await acknowledgeRequest;
  await page.getByRole('button', { name: 'Silence reading backlog for 24 hours' }).click();
  await expect(page.getByText(/underlying evidence and alert remain recorded/)).toBeVisible();
  const silenceRequest = page.waitForRequest((request) => request.method() === 'POST' && request.url().endsWith('/alerts/alert-backlog/silence'));
  await page.getByRole('button', { name: 'Silence 24 hours' }).click();
  await silenceRequest;
});

test('desktop Home visually matches the normative dashboard hierarchy', async ({ page }) => {
  await page.setViewportSize({ width: 1680, height: 946 });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Live Power Usage' })).toBeVisible();
  await expect(page.getByTestId('usage-chart')).toBeVisible();
  await expect(page.getByTestId('daily-chart')).toBeVisible();
  expect(await page.evaluate(() => window.innerWidth)).toBe(1680);
  expect(await page.locator('.dashboard-lower').evaluate((element) => getComputedStyle(element).display)).toBe('grid');
  const usageCard = await page.locator('.usage-chart-card').boundingBox();
  const sensorCard = await page.locator('.sensor-status-card').boundingBox();
  const lastCommittedRow = await page.getByText('Last committed', { exact: true }).locator('..').boundingBox();
  expect(usageCard?.height).toBeLessThanOrEqual(330);
  expect(sensorCard?.y).toBeGreaterThanOrEqual((usageCard?.y ?? 0) + (usageCard?.height ?? 0));
  expect(sensorCard?.y).toBeLessThan(720);
  expect((lastCommittedRow?.y ?? 0) + (lastCommittedRow?.height ?? 0)).toBeLessThanOrEqual((sensorCard?.y ?? 0) + (sensorCard?.height ?? 0));
  await expect(page).toHaveScreenshot('home-reference-1680x946.png', { fullPage: false });
});

test('tablet Home preserves the dashboard hierarchy', async ({ page }) => {
  await page.setViewportSize({ width: 834, height: 1112 });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Live Power Usage' })).toBeVisible();
  await expect(page.getByTestId('usage-chart')).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible();
  await expect(page).toHaveScreenshot('home-tablet-834x1112.png', { fullPage: false });
});

test('mobile Home remains readable and touch navigable', async ({ page }) => {
  await page.setViewportSize({ width: 412, height: 915 });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Live Power Usage' })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible();
  await expect(page).toHaveScreenshot('home-mobile-412x915.png', { fullPage: false });
});
