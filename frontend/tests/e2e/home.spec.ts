import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';
import { device, home } from '../fixtures';
import { mockApi } from './mocks';

test.beforeEach(async ({ page }) => { await mockApi(page); });

test('Home shows live readings, committed summaries and sensor evidence', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();
  await expect(page.locator('.dashboard-live-reading').getByText('2.48', { exact: true })).toBeVisible();
  await expect(page.getByText(/Live readings can appear before saved History because stored readings must be accepted by the server first/)).toBeVisible();
  const summary = page.locator('.dashboard-summary-card');
  await expect(summary.getByText('18.74 kWh')).toBeVisible();
  await expect(summary.getByText('$3.21')).toBeVisible();
  await expect(summary.getByText('Today Estimated Cost')).toBeVisible();
  await expect(summary.getByText('This Week Cost')).toBeVisible();
  await expect(summary.getByText('Current Rate')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Sensor health' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Voltage' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Current' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Frequency' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Power Factor' })).toHaveCount(0);
  await expect(page.getByText('Today Completeness')).toHaveCount(0);
  await expect(page.getByTestId('usage-chart')).toBeVisible();
  const usagePath = await page.locator('[data-testid="usage-chart"] .recharts-area-area').getAttribute('d');
  expect(usagePath?.match(/M/g)?.length).toBeGreaterThanOrEqual(2);
  await expect(page.getByRole('button', { name: /Main Panel Sensor/ })).toBeVisible();
});

test('Home preserves measured zero while showing missing voltage as unavailable', async ({ page }) => {
  await page.unrouteAll({ behavior: 'wait' });
  await mockApi(page, { homeOverride: { ...home, devices: [{ ...home.devices[0]!, measurement: { ...home.devices[0]!.measurement, active_power_w: 0, voltage_v: null } }] } });
  await page.goto('/');
  await expect(page.getByLabel('0 watts', { exact: true })).toBeVisible();
  const sensorRow = page.getByRole('rowheader', { name: /Main Panel Sensor/ }).locator('..');
  await expect(sensorRow.getByText('0 W')).toBeVisible();
  await expect(sensorRow.getByText('Not available')).toBeVisible();
});

test('Home totals a verified live aggregate and uses adaptive power units per sensor', async ({ page }) => {
  const indoor = { ...home.devices[0]!, measurement: { ...home.devices[0]!.measurement, active_power_w: 600 } };
  const outdoor = { ...home.devices[0]!, id: 'device-outdoor', friendly_name: 'Outdoor AC', measurement: { ...home.devices[0]!.measurement, active_power_w: 1400 } };
  await page.unrouteAll({ behavior: 'wait' });
  await mockApi(page, {
    homeOverride: {
      ...home,
      devices: [indoor, outdoor],
      summary_scope: { kind: 'verified_aggregate', device_id: null, device_ids: ['device-main', 'device-outdoor'], aggregate: true, circuit_id: 'circuit-aggregate' },
      aggregate_measurement: { state: 'live', active_power_w: 2000, member_device_ids: ['device-main', 'device-outdoor'], voltage_v: null, frequency_hz: null, power_factor: null },
    },
    devicesOverride: [device, { ...device, id: 'device-outdoor', friendly_name: 'Outdoor AC' }],
  });
  await page.goto('/');

  await expect(page.getByLabel('2 kilowatts', { exact: true })).toBeVisible();
  await expect(page.getByText('Main service combines live power from 2 non-overlapping sensors.')).toBeVisible();
  const indoorRow = page.getByRole('rowheader', { name: /Main Panel Sensor/ }).locator('..');
  const outdoorRow = page.getByRole('rowheader', { name: /Outdoor AC/ }).locator('..');
  await expect(indoorRow.getByText('600 W')).toBeVisible();
  await expect(outdoorRow.getByText('1.4 kW')).toBeVisible();
});

test('Home navigation stays limited to the four approved routes', async ({ page }) => {
  await page.goto('/');
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' }).filter({ visible: true });
  await expect(navigation.getByRole('link')).toHaveCount(4);
  await expect(navigation.getByRole('link', { name: 'Home' })).toBeVisible();
  await expect(navigation.getByRole('link', { name: 'History' })).toBeVisible();
  await expect(navigation.getByRole('link', { name: 'Billing' })).toBeVisible();
  await expect(navigation.getByRole('link', { name: 'Settings' })).toBeVisible();
  await expect(navigation.getByRole('link', { name: /Sensors|Alerts|Commands/ })).toHaveCount(0);
});

test('multiple sensors share one health surface without inventing unavailable values', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  const unavailableMeasurement = { voltage_v: null, current_a: null, active_power_w: null, frequency_hz: null, power_factor: null, measured_at: null, pzem_status: 'absent' };
  const outdoor = { ...home.devices[0]!, id: 'device-outdoor', friendly_name: 'Outdoor AC', state: 'offline', measurement: unavailableMeasurement, heartbeat_at: '2026-08-13T16:20:00Z', last_committed_at: null, storage_status: 'unavailable' };
  await page.unrouteAll({ behavior: 'wait' });
  await mockApi(page, {
    homeOverride: { ...home, devices: [home.devices[0]!, outdoor] },
    devicesOverride: [device, { ...device, id: 'device-outdoor', friendly_name: 'Outdoor AC', location: 'Outdoor unit', pzem_status: 'absent', storage_status: 'unavailable' }],
  });
  await page.goto('/');
  const health = page.getByRole('table', { name: 'Sensor health and live electrical measurements' });
  await expect(health.getByRole('row')).toHaveCount(2);
  const outdoorRow = health.getByRole('rowheader', { name: /Outdoor AC/ }).locator('..');
  await expect(outdoorRow.getByText('Offline')).toBeVisible();
  await expect(outdoorRow.getByText('Absent')).toBeVisible();
  await expect(outdoorRow.getByText('Unavailable')).toBeVisible();
  await expect(outdoorRow.getByText('Not available')).toHaveCount(6);
  await expect(outdoorRow.getByText(/0 W|0 V|0 A/)).toHaveCount(0);
  await expect(page).toHaveScreenshot('home-multiple-sensors-unavailable-1280x720.png', { fullPage: false, maxDiffPixelRatio: 0.03 });
});

test('current rate stays calm when no published rate exists', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.unrouteAll({ behavior: 'wait' });
  await mockApi(page, { homeOverride: { ...home, current_rate: null } });
  await page.goto('/');
  const rate = page.locator('.dashboard-summary-metric').filter({ hasText: 'Current Rate' });
  await expect(rate.getByText('—')).toBeVisible();
  await expect(rate.getByText('No published rate')).toBeVisible();
  await expect(page).toHaveScreenshot('home-no-published-rate-1024x768.png', { fullPage: false, maxDiffPixelRatio: 0.03 });
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
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Live Power Usage' })).toBeVisible();
  await expect(page.getByTestId('usage-chart')).toBeVisible();
  await expect(page.getByTestId('daily-chart')).toBeVisible();
  expect(await page.evaluate(() => window.innerWidth)).toBe(1440);
  const overview = await page.locator('.dashboard-overview').boundingBox();
  const sensorHealth = await page.locator('.dashboard-sensor-health').boundingBox();
  const content = await page.locator('.dashboard-content').boundingBox();
  expect(sensorHealth?.y).toBeGreaterThanOrEqual((overview?.y ?? 0) + (overview?.height ?? 0));
  expect(content?.y).toBeGreaterThanOrEqual((sensorHealth?.y ?? 0) + (sensorHealth?.height ?? 0));
  await expect(page).toHaveScreenshot('home-reference-1440x900.png', { fullPage: false, maxDiffPixelRatio: 0.03 });
});

test('tablet Home preserves the dashboard hierarchy', async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Live Power Usage' })).toBeVisible();
  await expect(page.getByTestId('usage-chart')).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible();
  await expect(page).toHaveScreenshot('home-tablet-768x1024.png', { fullPage: false, maxDiffPixelRatio: 0.03 });
});

test('mobile Home remains readable and touch navigable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Live Power Usage' })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible();
  await expect(page).toHaveScreenshot('home-mobile-390x844.png', { fullPage: false, maxDiffPixelRatio: 0.03 });
});

for (const viewport of [{ width: 1280, height: 720 }, { width: 1024, height: 768 }]) {
  test(`Home has no horizontal overflow at ${viewport.width}x${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  });
}
