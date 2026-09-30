import { expect, test } from '@playwright/test';
import { livePricing } from '../fixtures';
import { mockApi } from './mocks';

test('a clock-only price transition updates within two seconds without History traffic', async ({ page }, testInfo) => {
  await mockApi(page);
  let epoch = 0;
  let historyRequests = 0;
  const serverEpoch = Date.parse('2026-08-13T23:59:58Z');
  page.on('request', (request) => { if (new URL(request.url()).pathname.endsWith('/history')) historyRequests += 1; });
  await page.route('**/api/v1/home/pricing?*', async (route) => {
    epoch ||= Date.now();
    const elapsed = Date.now() - epoch;
    const transitioned = elapsed >= 2000;
    await route.fulfill({ json: {
      ...livePricing,
      generated_at: new Date(serverEpoch + elapsed).toISOString(),
      next_pricing_refresh_at: transitioned ? null : '2026-08-14T00:00:00Z',
      current_rate: { ...livePricing.current_rate,
        load_fresh_until: null, load_state: 'stale', estimated_cost_per_hour: null,
        price_per_kwh: transitioned ? '0.30' : '0.172', tou_period: transitioned ? 'Peak' : 'Off-Peak',
        next_change_at: transitioned ? null : '2026-08-14T00:00:00Z',
      },
    } });
  });
  await page.goto('/');
  const pricing = page.getByRole('region', { name: 'Current electricity pricing' });
  await expect(pricing.getByText('$0.172/kWh')).toBeVisible();
  await expect(page.getByTestId('usage-chart')).toBeVisible();
  const initialHistoryRequests = historyRequests;
  await expect(pricing.getByText('$0.30/kWh', { exact: true })).toBeVisible({ timeout: 4000 });
  const transitionLatencyMs = Date.now() - epoch - 2000;
  console.log(JSON.stringify({ scenario: 'clock-only-price-transition', browser: testInfo.project.name, transitionLatencyMs, historyRequestsDuringBoundary: historyRequests - initialHistoryRequests }));
  expect(transitionLatencyMs).toBeLessThanOrEqual(2000);
  expect(historyRequests).toBe(initialHistoryRequests);
  await expect(pricing.getByText('Peak', { exact: true })).toBeVisible();
  await expect(pricing.getByText(/Stale load/)).toBeVisible();
  await testInfo.attach('clock-only-transition.json', { body: JSON.stringify({ transitionLatencyMs, historyRequestsDuringBoundary: historyRequests - initialHistoryRequests, network: 'local intercepted API, no backend latency claim', browser: testInfo.project.name }), contentType: 'application/json' });
});

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test(`hybrid pricing and existing charts remain usable at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await mockApi(page);
    await page.route('**/api/v1/home/pricing?*', (route) => route.fulfill({ json: {
      ...livePricing,
      current_rate: { ...livePricing.current_rate, pricing_model: 'hybrid', price_per_kwh: '0.30',
        current_tier: 2, tou_period: 'Peak', cycle_usage_kwh: '10.5', tier_progress_percent: '100',
        estimated_cost_per_hour: '0.60', load_fresh_until: null,
      },
    } }));
    await page.goto('/');
    const pricing = page.getByRole('region', { name: 'Current electricity pricing' });
    await expect(pricing.getByText('Peak · Tier 2')).toBeVisible();
    await expect(pricing.getByText('$0.60/hour')).toBeVisible();
    await expect(page.getByTestId('usage-chart')).toBeAttached();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await testInfo.attach(`pricing-${viewport.width}.png`, { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
    await pricing.evaluate((element) => element.scrollIntoView({ block: 'center' }));
    await testInfo.attach(`pricing-detail-${viewport.width}.png`, { body: await pricing.screenshot(), contentType: 'image/png' });
  });
}
