import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';
import { device, home } from '../fixtures';
import { mockApi } from './mocks';

test.beforeEach(async ({ page }) => { await mockApi(page); });

test('Home shows live readings, a compact billing-cycle summary, and sensor evidence', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();
  await expect(page.locator('.dashboard-live-reading').getByText('2.48', { exact: true })).toBeVisible();
  await expect(page.getByText(/History may take a moment to show the newest accepted reading/)).toBeVisible();
  const summary = page.locator('.dashboard-summary-card');
  await expect(summary.getByRole('heading', { name: 'Billing Cycle' })).toBeVisible();
  await expect(summary.getByText('0.17 kWh')).toBeVisible();
  await expect(summary.getByText('Current Usage')).toBeVisible();
  await expect(summary.getByText('Cost to Date')).toBeVisible();
  await expect(summary.getByText('Current Tier')).toBeVisible();
  await expect(summary.getByText('Estimated Monthly Bill')).toBeVisible();
  await expect(summary.getByText('Some energy is estimated because reading coverage is 0.4%.')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Sensor health' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Voltage' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Current' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Frequency' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Power Factor' })).toHaveCount(0);
  await expect(page.getByText('Today Completeness')).toHaveCount(0);
  await expect(page.getByTestId('usage-chart')).toBeVisible();
  await expect(page.getByTestId('daily-chart')).toHaveAttribute('data-day-source', 'bounded-intervals');
  await expect(page.getByTestId('daily-chart')).toHaveAttribute('data-day-count', '2');
  await expect(page.getByTestId('daily-chart')).toHaveAttribute('data-unallocated-gap-count', '0');
  await expect(page.getByText('18.42 kWh')).toBeVisible();
  const usagePath = await page.locator('[data-testid="usage-chart"] .recharts-area-area').getAttribute('d');
  expect(usagePath?.match(/M/g)?.length).toBeGreaterThanOrEqual(2);
  await expect(page.getByRole('button', { name: /Main Panel Sensor/ })).toBeVisible();
});

test('Power History slider updates a readable non-overlapping selected range', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  const chart = page.getByTestId('usage-chart');
  const rangeLabel = page.getByTestId('power-selected-range');
  const original = await rangeLabel.innerText();
  const leftHandle = chart.locator('.recharts-brush-traveller').first();
  const brushSlide = chart.locator('.recharts-brush-slide');
  await leftHandle.dragTo(brushSlide, { targetPosition: { x: 150, y: 10 }, force: true });
  await expect.poll(() => rangeLabel.innerText()).not.toBe(original);
  const selected = await rangeLabel.innerText();
  await page.waitForTimeout(1_100);
  await expect(rangeLabel).toHaveText(selected);
  await expect(chart).toHaveAttribute('data-user-selected-range', 'true');
  await expect(page.getByRole('button', { name: 'Reset zoom' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Resume live' })).toBeVisible();
  const overlaps = await page.locator('.dashboard-power-history .chart-footer').evaluate((footer) => {
    const selectedRange = footer.querySelector('.chart-footer-range')?.getBoundingClientRect();
    const coverage = footer.querySelector('.chart-footer-coverage')?.getBoundingClientRect();
    if (!selectedRange || !coverage) return true;
    return selectedRange.left < coverage.right
      && selectedRange.right > coverage.left
      && selectedRange.top < coverage.bottom
      && selectedRange.bottom > coverage.top;
  });
  expect(overlaps).toBe(false);
  await page.getByRole('button', { name: 'Reset zoom' }).click();
  await expect(chart).toHaveAttribute('data-user-selected-range', 'false');
  await expect(page.getByRole('button', { name: 'Reset zoom' })).toHaveCount(0);
});

test('Power History keeps its selected timestamps through a five-second dashboard refresh without a white chart outline', async ({ page }) => {
  let homeRequests = 0;
  await page.addInitScript(() => {
    // Some browsers and input devices drive the Recharts brush through mouse
    // events without delivering Pointer Events to React. The mouse fallback
    // must still commit the user's selection before the next live refresh.
    window.addEventListener('pointerdown', (event) => event.stopImmediatePropagation(), true);
    class MeasurementEventSource extends EventTarget {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSED = 2;
      readonly CONNECTING = 0;
      readonly OPEN = 1;
      readonly CLOSED = 2;
      readonly readyState = 1;
      readonly url = '/api/v1/events';
      readonly withCredentials = true;
      onerror = null;
      onmessage = null;
      onopen = null;

      constructor() {
        super();
        window.setTimeout(() => this.dispatchEvent(new Event('measurement_accepted')), 5_000);
      }

      close() { /* The test emits one accepted measurement. */ }
    }
    window.EventSource = MeasurementEventSource as unknown as typeof EventSource;
  });
  await page.route('**/api/v1/home?**', async (route) => {
    homeRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ...home, generated_at: new Date(Date.parse(home.generated_at) + homeRequests * 5_000).toISOString() }),
    });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');

  const chart = page.getByTestId('usage-chart');
  const rangeLabel = page.getByTestId('power-selected-range');
  const leftHandle = chart.locator('.recharts-brush-traveller').first();
  const brushSlide = chart.locator('.recharts-brush-slide');
  await leftHandle.dragTo(brushSlide, { targetPosition: { x: 150, y: 10 }, force: true });
  await expect(chart).toHaveAttribute('data-user-selected-range', 'true');
  const selected = await rangeLabel.innerText();
  const selectedBrushBounds = await chart.locator('.recharts-brush-slide').evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return { x: bounds.x, width: bounds.width };
  });

  const surface = chart.locator('.recharts-surface');
  await surface.focus();
  await expect.poll(() => surface.evaluate((element) => getComputedStyle(element).outlineStyle)).toBe('none');

  await expect.poll(() => homeRequests, { timeout: 8_000 }).toBeGreaterThanOrEqual(2);
  await expect(rangeLabel).toHaveText(selected);
  await expect(chart).toHaveAttribute('data-user-selected-range', 'true');
  const refreshedBrushBounds = await chart.locator('.recharts-brush-slide').evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return { x: bounds.x, width: bounds.width };
  });
  expect(refreshedBrushBounds.x).toBeCloseTo(selectedBrushBounds.x, 0);
  expect(refreshedBrushBounds.width).toBeCloseTo(selectedBrushBounds.width, 0);
  await expect(page.getByRole('button', { name: 'Reset zoom' })).toBeVisible();
});

test('Power History five-second range interaction stays local and avoids disruptive long tasks', async ({ page }, testInfo) => {
  let historyRequests = 0;
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.endsWith('/history')) historyRequests += 1;
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  const chart = page.getByTestId('usage-chart');
  await expect(chart).toBeVisible();
  const leftHandle = chart.locator('.recharts-brush-traveller').first();
  const brushSlide = chart.locator('.recharts-brush-slide');
  const requestsBeforeDrag = historyRequests;
  const longTaskCountBefore = await page.evaluate(() => performance.getEntriesByType('longtask').length);
  const layoutShiftBefore = await page.evaluate(() => performance.getEntriesByType('layout-shift')
    .filter((entry) => !(entry as PerformanceEntry & { hadRecentInput?: boolean }).hadRecentInput)
    .reduce((total, entry) => total + Number((entry as PerformanceEntry & { value?: number }).value ?? 0), 0));
  await leftHandle.dragTo(brushSlide, { targetPosition: { x: 150, y: 10 }, force: true });
  await expect(chart).toHaveAttribute('data-user-selected-range', 'true');
  const dragStarted = performance.now();
  for (let step = 1; step <= 50; step += 1) {
    await leftHandle.press(step % 2 === 0 ? 'ArrowRight' : 'ArrowLeft');
    await page.waitForTimeout(100);
  }
  const dragDurationMs = performance.now() - dragStarted;
  const longTasks = await page.evaluate((offset) => performance.getEntriesByType('longtask').slice(offset).map((entry) => entry.duration), longTaskCountBefore);
  const layoutShiftAfter = await page.evaluate(() => performance.getEntriesByType('layout-shift')
    .filter((entry) => !(entry as PerformanceEntry & { hadRecentInput?: boolean }).hadRecentInput)
    .reduce((total, entry) => total + Number((entry as PerformanceEntry & { value?: number }).value ?? 0), 0));
  const evidence = { dragDurationMs, historyRequestsDuringDrag: historyRequests - requestsBeforeDrag, longTasks, layoutShiftDelta: layoutShiftAfter - layoutShiftBefore };
  await testInfo.attach('slider-performance.json', { body: JSON.stringify(evidence, null, 2), contentType: 'application/json' });
  expect(dragDurationMs).toBeGreaterThanOrEqual(4_900);
  expect(historyRequests).toBe(requestsBeforeDrag);
  expect(longTasks.every((duration) => duration < 250)).toBe(true);
  expect(layoutShiftAfter - layoutShiftBefore).toBeLessThan(0.01);
  await expect(chart).toHaveAttribute('data-user-selected-range', 'true');
  await expect(page.getByRole('button', { name: 'Reset zoom' })).toBeVisible();
});

test('Power History slider remains responsive through repeated drag adjustments', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  const chart = page.getByTestId('usage-chart');
  const leftHandle = chart.locator('.recharts-brush-traveller').first();
  await expect(leftHandle).toBeVisible();
  const initial = await leftHandle.boundingBox();
  const chartBox = await chart.boundingBox();
  expect(initial).not.toBeNull();
  expect(chartBox).not.toBeNull();

  const positions: number[] = [];
  for (const targetX of [180, 280, 380]) {
    await leftHandle.dragTo(chart, { targetPosition: { x: targetX, y: chartBox!.height - 14 }, force: true });
    const moved = await leftHandle.boundingBox();
    expect(moved).not.toBeNull();
    positions.push(moved!.x);
  }

  expect(positions.every((position, index) => index === 0 || position > positions[index - 1]!)).toBe(true);
  expect(positions.at(-1)! - initial!.x).toBeGreaterThan(200);
  await expect(chart).toHaveAttribute('data-user-selected-range', 'true');
  await expect(page.getByRole('button', { name: 'Reset zoom' })).toBeVisible();
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
  const outdoor = { ...home.devices[0]!, id: 'device-outdoor', friendly_name: 'Outdoor AC', state: 'offline', measurement: unavailableMeasurement, heartbeat_at: '2026-08-13T16:20:00Z', last_committed_at: null, server_delivery_status: 'unavailable', last_server_received_at: null };
  await page.unrouteAll({ behavior: 'wait' });
  await mockApi(page, {
    homeOverride: { ...home, devices: [home.devices[0]!, outdoor] },
    devicesOverride: [device, { ...device, id: 'device-outdoor', friendly_name: 'Outdoor AC', location: 'Outdoor unit', pzem_status: 'absent', server_delivery_status: 'unavailable', last_server_received_at: null }],
  });
  await page.goto('/');
  const health = page.getByRole('table', { name: 'Sensor health and live electrical measurements' });
  await expect(health.getByRole('row')).toHaveCount(2);
  const outdoorRow = health.getByRole('rowheader', { name: /Outdoor AC/ }).locator('..');
  await expect(outdoorRow.getByText('Offline')).toBeVisible();
  await expect(outdoorRow.getByText('Absent')).toBeVisible();
  await expect(outdoorRow.getByText('Unavailable')).toBeVisible();
  await expect(outdoorRow.getByText('Not available')).toHaveCount(5);
  await expect(outdoorRow.getByText(/0 W|0 V|0 A/)).toHaveCount(0);
  await expect(page).toHaveScreenshot('home-multiple-sensors-unavailable-1280x720.png', { fullPage: false, maxDiffPixelRatio: 0.03 });
});

test('monthly projection stays calm when there is not enough data', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.unrouteAll({ behavior: 'wait' });
  await mockApi(page, { homeOverride: { ...home, current_rate: null } });
  await page.goto('/');
  const projection = page.locator('.dashboard-summary-metric').filter({ hasText: 'Estimated Monthly Bill' });
  await expect(projection.getByText('Not available')).toBeVisible();
  await expect(projection.getByText(/24 hours/)).toBeVisible();
  await expect(page).toHaveScreenshot('home-no-published-rate-1024x768.png', { fullPage: false, maxDiffPixelRatio: 0.03 });
});

test('sensor drawer exposes only reboot and signed OTA commands', async ({ page }) => {
  const commands = await mockApi(page);
  await page.goto('/');
  await page.getByRole('button', { name: /Main Panel Sensor/ }).click();
  await page.getByRole('button', { name: /Reboot/ }).last().click();
  await expect(page.getByText(/Measurements pause briefly/)).toBeVisible();
  await page.getByRole('button', { name: 'Reboot sensor' }).click();
  await expect.poll(() => commands).toContain('reboot');
  await expect(page.getByRole('link', { name: /Install OTA/ })).toBeVisible();
  await expect(page.getByText(/microSD|backlog|sync now|format storage/i)).toHaveCount(0);
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
  await page.getByRole('button', { name: 'Reboot sensor' }).click();
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
  await expect(alertDialog.getByRole('heading', { name: 'sensor delivery delayed' })).toBeVisible();
  const acknowledgeRequest = page.waitForRequest((request) => request.method() === 'POST' && request.url().endsWith('/alerts/alert-delivery/acknowledge'));
  await page.getByRole('button', { name: 'Acknowledge sensor delivery delayed' }).click();
  await acknowledgeRequest;
  await page.getByRole('button', { name: 'Silence sensor delivery delayed for 24 hours' }).click();
  await expect(page.getByText(/underlying evidence and alert remain recorded/)).toBeVisible();
  const silenceRequest = page.waitForRequest((request) => request.method() === 'POST' && request.url().endsWith('/alerts/alert-delivery/silence'));
  await page.getByRole('button', { name: 'Silence 24 hours' }).click();
  await silenceRequest;
  const dismissRequest = page.waitForRequest((request) => request.method() === 'DELETE' && request.url().endsWith('/alerts/alert-delivery/notification'));
  await page.getByRole('button', { name: 'Remove sensor delivery delayed notification' }).click();
  await expect(page.getByText(/only from your account/)).toBeVisible();
  await page.getByRole('button', { name: 'Remove notification' }).click();
  await dismissRequest;
  const clearAllRequest = page.waitForRequest((request) => request.method() === 'DELETE' && request.url().endsWith('/alerts/notifications'));
  await page.getByRole('button', { name: 'Clear all', exact: true }).click();
  await expect(page.getByText(/lifecycle history remain recorded/)).toBeVisible();
  await page.getByRole('button', { name: 'Clear all', exact: true }).click();
  await clearAllRequest;
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
