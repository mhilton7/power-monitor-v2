import { expect, test, type Locator, type Page } from '@playwright/test';
import { history, home } from '../fixtures';
import { mockApi } from './mocks';

type TimestampRange = { startMs: number; endMs: number };

test.use({ trace: 'on' });

test.beforeEach(async ({ page }) => {
  await mockApi(page);
});

async function installControllableLiveUpdates(page: Page) {
  await page.addInitScript(() => {
    class ControlledEventSource extends EventTarget {
      static activeSource: ControlledEventSource | null = null;
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
        ControlledEventSource.activeSource = this;
      }

      close() { ControlledEventSource.activeSource = null; }
    }
    (window as Window & { emitAcceptedRangeReading?: () => void }).emitAcceptedRangeReading = () => {
      ControlledEventSource.activeSource?.dispatchEvent(new Event('measurement_accepted'));
    };
    window.EventSource = ControlledEventSource as unknown as typeof EventSource;
  });
}

async function emitAcceptedReading(page: Page) {
  await page.evaluate(() => {
    (window as Window & { emitAcceptedRangeReading?: () => void }).emitAcceptedRangeReading?.();
  });
}

async function readRange(range: Locator): Promise<TimestampRange> {
  const start = await range.getAttribute('data-start-ms');
  const end = await range.getAttribute('data-end-ms');
  expect(start).not.toBeNull();
  expect(end).not.toBeNull();
  return { startMs: Number(start), endMs: Number(end) };
}

async function expectRange(range: Locator, expected: TimestampRange) {
  await expect(range).toHaveAttribute('data-start-ms', String(expected.startMs));
  await expect(range).toHaveAttribute('data-end-ms', String(expected.endMs));
}

async function dragBy(page: Page, part: Locator, deltaX: number, steps = 12) {
  await part.scrollIntoViewIfNeeded();
  const box = await part.boundingBox();
  expect(box).not.toBeNull();
  const startX = box!.x + box!.width / 2;
  const y = box!.y + box!.height / 2;
  await page.mouse.move(startX, y);
  await page.mouse.down();
  await page.mouse.move(startX + deltaX, y, { steps });
  await page.mouse.up();
}

async function dragByTrackFraction(page: Page, part: Locator, track: Locator, fraction: number) {
  const trackBox = await track.boundingBox();
  expect(trackBox).not.toBeNull();
  await dragBy(page, part, trackBox!.width * fraction);
}

async function touchDragBy(part: Locator, deltaX: number) {
  await part.scrollIntoViewIfNeeded();
  const box = await part.boundingBox();
  expect(box).not.toBeNull();
  const startX = box!.x + box!.width / 2;
  const y = box!.y + box!.height / 2;
  const common = { pointerId: 71, pointerType: 'touch', isPrimary: true };
  await part.dispatchEvent('pointerdown', { ...common, button: 0, buttons: 1, clientX: startX, clientY: y });
  await part.dispatchEvent('pointermove', { ...common, button: 0, buttons: 1, clientX: startX + deltaX, clientY: y });
  await part.dispatchEvent('pointerup', { ...common, button: 0, buttons: 0, clientX: startX + deltaX, clientY: y });
}

function duration(range: TimestampRange) {
  return range.endMs - range.startMs;
}

test('History 24 hours keeps both resized edges and a moved window through ticks, delayed refresh, tooltip, and resize', async ({ page }) => {
  test.slow();
  await installControllableLiveUpdates(page);
  let historyRequests = 0;
  let delayNextHistoryResponse = false;
  let refreshedResponseDelivered = false;
  await page.route('**/api/v1/history**', async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('metric') !== 'power') {
      await route.fallback();
      return;
    }
    historyRequests += 1;
    if (delayNextHistoryResponse) {
      delayNextHistoryResponse = false;
      await new Promise((resolve) => setTimeout(resolve, 750));
      refreshedResponseDelivered = true;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ...history,
        resolution_seconds: Number(url.searchParams.get('resolution_seconds') ?? history.resolution_seconds),
        completeness: refreshedResponseDelivered ? 0.81 : history.completeness,
        points: refreshedResponseDelivered
          ? [...history.points, { timestamp: '2026-08-13T17:31:00Z', value: 2.61, cost: '0.19', quality: 0.81 }]
          : history.points,
      }),
    });
  });

  await page.goto('/history');
  await page.getByRole('button', { name: '24 hours' }).click();
  await expect(page.getByRole('button', { name: '24 hours' })).toHaveAttribute('aria-pressed', 'true');
  const chart = page.getByTestId('history-chart');
  const range = page.getByTestId('history-selected-range');
  const track = page.getByTestId('history-range-track');
  const start = page.getByTestId('history-range-start');
  const end = page.getByTestId('history-range-end');
  const windowControl = page.getByTestId('history-range-window');
  await expect(chart).toHaveAttribute('data-range-mode', 'auto');
  const full = await readRange(range);
  expect(duration(full)).toBe(24 * 3_600_000);
  const fullTickLabels = await chart.locator('.recharts-xAxis-tick-labels text').allTextContents();

  await dragByTrackFraction(page, start, track, 0.25);
  const leftResized = await readRange(range);
  expect(leftResized.startMs).toBeGreaterThan(full.startMs);
  expect(leftResized.endMs).toBe(full.endMs);
  await expect(chart).toHaveAttribute('data-range-mode', 'manual');
  await expect.poll(() => chart.locator('.recharts-xAxis-tick-labels text').allTextContents()).not.toEqual(fullTickLabels);

  await page.waitForTimeout(15_100);
  await expectRange(range, leftResized);

  delayNextHistoryResponse = true;
  const requestsBeforeRefresh = historyRequests;
  await emitAcceptedReading(page);
  await expect.poll(() => historyRequests).toBeGreaterThan(requestsBeforeRefresh);

  await dragByTrackFraction(page, end, track, -0.2);
  const bothResized = await readRange(range);
  expect(bothResized.startMs).toBe(leftResized.startMs);
  expect(bothResized.endMs).toBeLessThan(leftResized.endMs);

  await dragByTrackFraction(page, windowControl, track, 0.08);
  const moved = await readRange(range);
  expect(moved.startMs).toBeGreaterThan(bothResized.startMs);
  expect(moved.endMs).toBeGreaterThan(bothResized.endMs);
  expect(duration(moved)).toBe(duration(bothResized));

  await expect.poll(() => refreshedResponseDelivered).toBe(true);
  await expectRange(range, moved);
  await expect(page.locator('.history-summary-grid')).toContainText('81%');

  await chart.locator('.recharts-wrapper').hover({ position: { x: 180, y: 120 }, force: true });
  await expectRange(range, moved);
  await page.setViewportSize({ width: 1024, height: 768 });
  await expectRange(range, moved);
  await page.getByRole('button', { name: '24 hours' }).click();
  await expectRange(range, moved);

  await page.getByRole('button', { name: 'Reset zoom' }).click();
  await expect(chart).toHaveAttribute('data-range-mode', 'auto');
  await expectRange(range, full);
});

test('History 7 days resets once on preset change, then survives both thumbs, window movement, 15 seconds, and refresh', async ({ page }) => {
  test.slow();
  await installControllableLiveUpdates(page);
  let historyRequests = 0;
  await page.route('**/api/v1/history**', async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('metric') !== 'power') {
      await route.fallback();
      return;
    }
    historyRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ...history,
        resolution_seconds: Number(url.searchParams.get('resolution_seconds') ?? history.resolution_seconds),
        completeness: historyRequests > 1 ? 0.77 : history.completeness,
      }),
    });
  });

  await page.goto('/history');
  await page.getByRole('button', { name: '24 hours' }).click();
  await page.getByTestId('history-range-start').press('PageUp');
  await expect(page.getByTestId('history-chart')).toHaveAttribute('data-range-mode', 'manual');

  await page.getByRole('button', { name: '7 days' }).click();
  await expect(page.getByRole('button', { name: '7 days' })).toHaveAttribute('aria-pressed', 'true');
  const chart = page.getByTestId('history-chart');
  const range = page.getByTestId('history-selected-range');
  const track = page.getByTestId('history-range-track');
  const full = await readRange(range);
  await expect(chart).toHaveAttribute('data-range-mode', 'auto');
  expect(duration(full)).toBe(7 * 86_400_000);

  await dragByTrackFraction(page, page.getByTestId('history-range-start'), track, 0.2);
  const left = await readRange(range);
  expect(left.endMs).toBe(full.endMs);
  await dragByTrackFraction(page, page.getByTestId('history-range-end'), track, -0.2);
  const resized = await readRange(range);
  expect(resized.startMs).toBe(left.startMs);
  await dragByTrackFraction(page, page.getByTestId('history-range-window'), track, 0.1);
  const moved = await readRange(range);
  expect(duration(moved)).toBe(duration(resized));

  await page.waitForTimeout(15_100);
  await expectRange(range, moved);
  const requestsBeforeRefresh = historyRequests;
  await emitAcceptedReading(page);
  await expect.poll(() => historyRequests).toBeGreaterThan(requestsBeforeRefresh);
  await expectRange(range, moved);
  await page.getByRole('button', { name: '7 days' }).click();
  await expectRange(range, moved);
});

test('Dashboard keeps timestamp range through live append and preserves window duration until Resume live', async ({ page }) => {
  await installControllableLiveUpdates(page);
  let homeRequests = 0;
  let historyRequests = 0;
  await page.route('**/api/v1/home?**', async (route) => {
    homeRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ...home, generated_at: new Date(Date.parse(home.generated_at) + homeRequests * 5_000).toISOString() }),
    });
  });
  await page.route('**/api/v1/history**', async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('metric') !== 'power') {
      await route.fallback();
      return;
    }
    historyRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ...history,
        resolution_seconds: Number(url.searchParams.get('resolution_seconds') ?? history.resolution_seconds),
        completeness: historyRequests > 1 ? 0.84 : history.completeness,
        points: historyRequests > 1
          ? [...history.points, { timestamp: '2026-08-13T17:31:30Z', value: 2.7, cost: '0.20', quality: 0.84 }]
          : history.points,
      }),
    });
  });

  await page.goto('/');
  const chart = page.getByTestId('usage-chart');
  const range = page.getByTestId('power-selected-range');
  const track = page.getByTestId('power-range-track');
  const full = await readRange(range);
  await expect(chart).toHaveAttribute('data-range-mode', 'auto');
  await chart.evaluate((element) => { (element as HTMLElement & { rangeMountToken?: string }).rangeMountToken = 'stable'; });

  await dragByTrackFraction(page, page.getByTestId('power-range-start'), track, 0.2);
  const left = await readRange(range);
  expect(left.endMs).toBe(full.endMs);
  await dragByTrackFraction(page, page.getByTestId('power-range-end'), track, -0.2);
  const resized = await readRange(range);
  expect(resized.startMs).toBe(left.startMs);
  await dragByTrackFraction(page, page.getByTestId('power-range-window'), track, 0.1);
  const moved = await readRange(range);
  expect(duration(moved)).toBe(duration(resized));
  await expect(chart).toHaveAttribute('data-range-mode', 'manual');

  await page.waitForTimeout(3_100);
  await expectRange(range, moved);
  const homeBefore = homeRequests;
  const historyBefore = historyRequests;
  await emitAcceptedReading(page);
  await expect.poll(() => homeRequests).toBeGreaterThan(homeBefore);
  await expect.poll(() => historyRequests).toBeGreaterThan(historyBefore);
  await expectRange(range, moved);
  expect(await chart.evaluate((element) => (element as HTMLElement & { rangeMountToken?: string }).rangeMountToken)).toBe('stable');

  await chart.locator('.recharts-wrapper').hover({ position: { x: 220, y: 90 }, force: true });
  await expectRange(range, moved);
  await page.setViewportSize({ width: 768, height: 1024 });
  await expectRange(range, moved);

  await page.getByRole('button', { name: 'Resume live' }).click();
  await expect(chart).toHaveAttribute('data-range-mode', 'auto');
  const resumed = await readRange(range);
  expect(resumed.endMs).toBeGreaterThan(moved.endMs);
  expect(duration(resumed)).toBe(duration(full));
});

test('mobile touch drag and orientation change preserve the committed Dashboard timestamps', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const range = page.getByTestId('power-selected-range');
  const track = page.getByTestId('power-range-track');
  const startControl = page.getByTestId('power-range-start');
  const endControl = page.getByTestId('power-range-end');
  const windowControl = page.getByTestId('power-range-window');
  await startControl.press('End');
  const narrowStartBox = await startControl.boundingBox();
  const narrowEndBox = await endControl.boundingBox();
  const trackBox = await track.boundingBox();
  expect(narrowStartBox).not.toBeNull();
  expect(narrowEndBox).not.toBeNull();
  expect(trackBox).not.toBeNull();
  expect(narrowStartBox!.x + narrowStartBox!.width).toBeLessThanOrEqual(narrowEndBox!.x + 1);
  const narrow = await readRange(range);
  await touchDragBy(windowControl, -trackBox!.width * 0.15);
  const narrowMoved = await readRange(range);
  expect(duration(narrowMoved)).toBe(duration(narrow));
  expect(narrowMoved.startMs).toBeLessThan(narrow.startMs);
  await page.getByRole('button', { name: 'Reset zoom' }).click();
  await touchDragBy(startControl, trackBox!.width * 0.25);
  const touched = await readRange(range);
  await expect(page.getByTestId('usage-chart')).toHaveAttribute('data-range-mode', 'manual');
  await page.setViewportSize({ width: 844, height: 390 });
  await expectRange(range, touched);

  await windowControl.press('PageDown');
  const keyboardMoved = await readRange(range);
  expect(duration(keyboardMoved)).toBe(duration(touched));
  expect(keyboardMoved.startMs).not.toBe(touched.startMs);
  await expect(windowControl).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(startControl).toBeFocused();
  await expect(startControl).toHaveCSS('outline-style', 'solid');
});

test('five-second selected-window drag stays local, keeps the chart mounted, and records performance evidence', async ({ page, browserName }, testInfo) => {
  test.skip(browserName !== 'chromium', 'Chromium exposes the performance evidence used by this regression test.');
  await installControllableLiveUpdates(page);
  let homeRequests = 0;
  let historyRequests = 0;
  await page.route('**/api/v1/home?**', async (route) => {
    homeRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ...home,
        generated_at: new Date(Date.parse(home.generated_at) + homeRequests * 5_000).toISOString(),
      }),
    });
  });
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.endsWith('/history')) historyRequests += 1;
  });
  await page.addInitScript(() => {
    const evidence = { longTasks: [] as number[], layoutShifts: [] as number[] };
    (window as Window & { rangePerformanceEvidence?: typeof evidence }).rangePerformanceEvidence = evidence;
    try {
      new PerformanceObserver((list) => evidence.longTasks.push(...list.getEntries().map((entry) => entry.duration))).observe({ type: 'longtask', buffered: true });
      new PerformanceObserver((list) => evidence.layoutShifts.push(...list.getEntries()
        .filter((entry) => !(entry as PerformanceEntry & { hadRecentInput?: boolean }).hadRecentInput)
        .map((entry) => Number((entry as PerformanceEntry & { value?: number }).value ?? 0)))).observe({ type: 'layout-shift', buffered: true });
    } catch {
      // Older engines may omit one of these optional performance entry types.
    }
  });
  await page.goto('/');
  const chart = page.getByTestId('usage-chart');
  const range = page.getByTestId('power-selected-range');
  const track = page.getByTestId('power-range-track');
  await dragByTrackFraction(page, page.getByTestId('power-range-start'), track, 0.25);
  await dragByTrackFraction(page, page.getByTestId('power-range-end'), track, -0.2);
  const before = await readRange(range);
  await chart.evaluate((element) => { (element as HTMLElement & { rangeMountToken?: string }).rangeMountToken = 'performance'; });
  const evidenceStart = await page.evaluate(() => {
    const evidence = (window as Window & { rangePerformanceEvidence?: { longTasks: number[]; layoutShifts: number[] } }).rangePerformanceEvidence;
    return { longTaskCount: evidence?.longTasks.length ?? 0, layoutShiftCount: evidence?.layoutShifts.length ?? 0 };
  });

  const windowControl = page.getByTestId('power-range-window');
  const box = await windowControl.boundingBox();
  expect(box).not.toBeNull();
  const startX = box!.x + box!.width / 2;
  const y = box!.y + box!.height / 2;
  const requestsBeforeDrag = historyRequests;
  const startedAt = Date.now();
  await page.mouse.move(startX, y);
  await page.mouse.down();
  for (let step = 0; step < 100; step += 1) {
    const offset = 35 * Math.sin((step / 99) * Math.PI * 3);
    await page.mouse.move(startX + offset, y);
    await page.waitForTimeout(50);
  }
  const requestsBeforeNormalRefresh = historyRequests;
  expect(requestsBeforeNormalRefresh - requestsBeforeDrag).toBe(0);
  const homeBeforeRefresh = homeRequests;
  await emitAcceptedReading(page);
  await expect.poll(() => homeRequests).toBeGreaterThan(homeBeforeRefresh);
  await expect.poll(() => historyRequests).toBeGreaterThan(requestsBeforeNormalRefresh);
  await page.waitForTimeout(500);
  const requestsAfterNormalRefresh = historyRequests;
  await page.mouse.move(startX + 20, y);
  await page.waitForTimeout(100);
  const requestsAfterPostRefreshMove = historyRequests;
  await page.mouse.up();
  const dragDurationMs = Date.now() - startedAt;
  const after = await readRange(range);
  const normalRefreshRequestsDuringDrag = requestsAfterNormalRefresh - requestsBeforeNormalRefresh;
  const requestsDuringDrag = (requestsBeforeNormalRefresh - requestsBeforeDrag)
    + (requestsAfterPostRefreshMove - requestsAfterNormalRefresh);
  expect(duration(after)).toBe(duration(before));
  expect(requestsDuringDrag).toBe(0);
  expect(normalRefreshRequestsDuringDrag).toBeGreaterThan(0);
  expect(dragDurationMs).toBeGreaterThanOrEqual(4_900);
  expect(await chart.evaluate((element) => (element as HTMLElement & { rangeMountToken?: string }).rangeMountToken)).toBe('performance');

  const refreshRequestsBefore = historyRequests;
  await emitAcceptedReading(page);
  await expect.poll(() => historyRequests).toBeGreaterThan(refreshRequestsBefore);
  await expectRange(range, after);
  const browserEvidence = await page.evaluate((start) => {
    const evidence = (window as Window & { rangePerformanceEvidence?: { longTasks: number[]; layoutShifts: number[] } }).rangePerformanceEvidence ?? { longTasks: [], layoutShifts: [] };
    return {
      longTasks: evidence.longTasks.slice(start.longTaskCount),
      layoutShifts: evidence.layoutShifts.slice(start.layoutShiftCount),
    };
  }, evidenceStart);
  const evidence = { dragDurationMs, requestsDuringDrag, normalRefreshRequestsDuringDrag, ...browserEvidence };
  await testInfo.attach('range-slider-performance.json', { body: JSON.stringify(evidence, null, 2), contentType: 'application/json' });
  expect(evidence.longTasks.filter((task) => task > 50).length).toBeLessThanOrEqual(1);
  expect(evidence.layoutShifts.reduce((total, value) => total + value, 0)).toBeLessThan(0.02);
});
