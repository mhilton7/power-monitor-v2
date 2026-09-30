import { expect, test, type Page } from '@playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { circuits, device, history, home, homeScopes, livePricing } from '../fixtures';
import { mockApi } from './mocks';

// Opt-in only: production timings must not compete with parallel CI tests.
const run = process.env.PM_CHART_BENCHMARK;
const directory = path.resolve('../.test-runtime/chart-performance', run ?? 'disabled');
const baseTime = Date.parse('2026-08-13T17:32:15Z');
const extra = process.env.PM_BENCH_EXTRA === '1';
type RequestEvidence = { metric: string; start: number; duration: number; bytes: number; points: number; url: string };

async function install(page: Page, sensors: number) {
  const devices = Array.from({ length: sensors }, (_, i) => ({ ...device, id: `device-${i}`, friendly_name: `Synthetic sensor ${i + 1}` }));
  await mockApi(page, { devicesOverride: devices, fixedTime: false });
  // Shift only the calendar. Native performance/timers/rAF must remain native
  // for actual browser timing; Playwright clock emulation replaces those APIs.
  await page.addInitScript((start) => {
    const NativeDate = Date;
    const origin = NativeDate.now();
    const now = () => start + NativeDate.now() - origin;
    window.Date = new Proxy(NativeDate, {
      construct: (target, args) => Reflect.construct(target, args.length ? args : [now()]) as Date,
      apply: () => new NativeDate(now()).toString(),
      get: (target, property, receiver) => property === 'now' ? now : Reflect.get(target, property, receiver) as unknown,
    });
  }, baseTime);
  const wallStart = Date.now();
  const fixtureNow = () => baseTime + Date.now() - wallStart;
  const requests: RequestEvidence[] = [];
  let homeRequests = 0;
  await page.route('**/api/v1/circuits**', (route) => route.fulfill({ json: { circuits: circuits.circuits.map((circuit) => ({ ...circuit, device_ids: devices.map((entry) => entry.id) })) } }));
  await page.route('**/api/v1/home?**', (route) => {
    homeRequests += 1;
    return route.fulfill({ json: {
      ...home,
      generated_at: new Date(fixtureNow()).toISOString(),
      devices: devices.map((entry, index) => ({ ...home.devices[0], id: entry.id, friendly_name: entry.friendly_name,
        measurement: { ...home.devices[0]!.measurement, active_power_w: 100 + index * 13 + homeRequests } })),
      summary_scope: { kind: 'verified_sum', device_id: null, circuit_id: circuits.circuits[0]!.id, aggregate: true },
    } });
  });
  await page.route('**/api/v1/home/pricing?**', (route) => {
    const now = fixtureNow();
    return route.fulfill({ json: { ...livePricing, generated_at: new Date(now).toISOString(), current_rate: {
      ...livePricing.current_rate, evaluated_at: new Date(now).toISOString(),
      load_measured_at: new Date(now - 1000).toISOString(), load_fresh_until: new Date(now + 30_000).toISOString(),
    } } });
  });
  await page.route('**/api/v1/history?**', async (route) => {
    const started = performance.now();
    const url = new URL(route.request().url());
    const from = Date.parse(url.searchParams.get('from')!);
    const to = Date.parse(url.searchParams.get('to')!);
    const resolution = Number(url.searchParams.get('resolution_seconds') ?? 300);
    const metric = url.searchParams.get('metric') ?? 'power';
    const count = Math.min(4000, Math.ceil((to - from) / (resolution * 1000)));
    const step = (to - from) / count;
    const points = Array.from({ length: count }, (_, i) => {
      const gap = i % 127 >= 117;
      const value = i % 89 === 0 ? 0 : 1 + Math.sin(i / 8) + (i % 43 === 0 ? 3 : 0);
      return { timestamp: new Date(from + i * step).toISOString(), value: gap ? null : metric === 'energy' ? value * resolution / 3600 : value, cost: gap ? null : '0.04', quality: gap ? 0 : 1 };
    });
    const body = JSON.stringify({ ...history, points, resolution_seconds: resolution, connection_gaps: [],
      missing_ranges: [{ start: new Date(from + 117 * step).toISOString(), end: new Date(from + 127 * step).toISOString() }],
      scope: { device_ids: devices.map((entry) => entry.id), aggregate: true },
    });
    await new Promise((resolve) => setTimeout(resolve, 80));
    try { await route.fulfill({ status: 200, contentType: 'application/json', body }); } catch { /* Cancellation is measured by browser requests separately. */ }
    requests.push({ metric, start: started, duration: performance.now() - started, bytes: Buffer.byteLength(body), points: count, url: url.pathname + url.search });
  });
  await page.addInitScript(() => {
    const evidence = { longTasks: [] as { start: number; duration: number }[], frames: [] as number[], inputPaint: [] as number[], commitPaint: [] as number[], svgMutations: 0, chartMounts: 0, resizeCallbacks: 0, activeSources: 0 };
    Object.assign(window, { chartBenchmark: evidence });
    new PerformanceObserver((list) => evidence.longTasks.push(...list.getEntries().map((entry) => ({ start: entry.startTime, duration: entry.duration })))).observe({ type: 'longtask', buffered: true });
    const NativeResizeObserver = window.ResizeObserver;
    window.ResizeObserver = class extends NativeResizeObserver {
      constructor(callback: ResizeObserverCallback) { super((entries, observer) => { evidence.resizeCallbacks += 1; callback(entries, observer); }); }
    };
    let prior = performance.now();
    const frame = (now: number) => { evidence.frames.push(now - prior); if (evidence.frames.length > 40000) evidence.frames.shift(); prior = now; requestAnimationFrame(frame); };
    requestAnimationFrame(frame);
    let inputStart: number | null = null;
    let commitStart: number | null = null;
    document.addEventListener('pointermove', (event) => { if (event.buttons && (event.target as Element).closest('.chart-range-selector')) inputStart = performance.now(); }, true);
    for (const type of ['pointerup', 'keydown']) document.addEventListener(type, (event) => {
      if ((event.target as Element).closest('.chart-range-selector')) commitStart = performance.now();
    }, true);
    new MutationObserver((records) => {
      for (const record of records) {
        if ((record.attributeName === 'data-start-ms' || record.attributeName === 'data-end-ms') && commitStart !== null) {
          const start = commitStart; commitStart = null;
          requestAnimationFrame(() => requestAnimationFrame(() => evidence.commitPaint.push(performance.now() - start)));
        }
        if (record.target instanceof Element && record.target.closest('.recharts-wrapper')) {
          evidence.svgMutations += 1;
          if (record.attributeName === 'd' && commitStart !== null) {
            const start = commitStart; commitStart = null;
            requestAnimationFrame(() => requestAnimationFrame(() => evidence.commitPaint.push(performance.now() - start)));
          }
        }
        if (record.type === 'attributes' && record.attributeName === 'data-selection-start' && inputStart !== null) {
          const start = inputStart; inputStart = null;
          requestAnimationFrame(() => requestAnimationFrame(() => evidence.inputPaint.push(performance.now() - start)));
        }
        for (const added of record.addedNodes) if (added instanceof Element) evidence.chartMounts += added.matches('.recharts-wrapper') ? 1 : added.querySelectorAll('.recharts-wrapper').length;
      }
    }).observe(document, { subtree: true, childList: true, attributes: true, attributeFilter: ['d', 'width', 'height', 'data-selection-start', 'data-start-ms', 'data-end-ms'] });
    class ControlledEventSource extends EventTarget {
      static source: ControlledEventSource | null = null;
      static readonly CONNECTING = 0; static readonly OPEN = 1; static readonly CLOSED = 2;
      readonly CONNECTING = 0; readonly OPEN = 1; readonly CLOSED = 2;
      readonly readyState = 1; readonly url = '/api/v1/events'; readonly withCredentials = true;
      onerror = null; onmessage = null; onopen = null;
      constructor() { super(); ControlledEventSource.source = this; evidence.activeSources += 1; }
      close() { if (ControlledEventSource.source === this) { ControlledEventSource.source = null; evidence.activeSources -= 1; } }
    }
    window.EventSource = ControlledEventSource as unknown as typeof EventSource;
    Object.assign(window, { emitBenchmarkEvent: (type: string) => ControlledEventSource.source?.dispatchEvent(new MessageEvent(type, { data: JSON.stringify({ home_id: '00000000-0000-0000-0000-000000000010' }) })) });
  });
  return { requests, homeRequests: () => homeRequests };
}

async function emit(page: Page, type: string) {
  await page.evaluate((event) => (window as unknown as { emitBenchmarkEvent: (type: string) => void }).emitBenchmarkEvent(event), type);
}

async function snapshot(page: Page) {
  return page.evaluate(() => ({
    ...(window as unknown as { chartBenchmark: Record<string, unknown> }).chartBenchmark,
    domNodes: document.querySelectorAll('*').length,
    chartInstances: document.querySelectorAll('.recharts-wrapper').length,
    svgPathCharacters: [...document.querySelectorAll('.recharts-curve')].reduce((n, entry) => n + (entry.getAttribute('d')?.length ?? 0), 0),
    heap: (performance as unknown as { memory?: { usedJSHeapSize: number } }).memory?.usedJSHeapSize ?? null,
  }));
}

async function exercise(page: Page, prefix: 'power' | 'history', mobile: boolean, commitAtEnd = false) {
  const startMs = await page.evaluate(() => performance.now());
  await page.getByTestId(`${prefix}-range-start`).press('PageUp');
  await page.getByTestId(`${prefix}-range-end`).press('PageDown');
  const control = page.getByTestId(`${prefix}-range-window`);
  await control.scrollIntoViewIfNeeded();
  const box = (await control.boundingBox())!;
  const x = box.x + box.width / 2; const y = box.y + box.height / 2;
  const props = { pointerId: 19, pointerType: mobile ? 'touch' : 'mouse', isPrimary: true, button: 0, buttons: 1 };
  if (mobile) await control.dispatchEvent('pointerdown', { ...props, clientX: x, clientY: y });
  else { await page.mouse.move(x, y); await page.mouse.down(); }
  for (let i = 0; i < 40; i += 1) {
    const nextX = x + (commitAtEnd && i === 39 ? 5 : 14 * Math.sin(i / 39 * Math.PI * 3));
    if (mobile) await control.dispatchEvent('pointermove', { ...props, clientX: nextX, clientY: y });
    else await page.mouse.move(nextX, y);
    await page.waitForTimeout(35);
  }
  if (mobile) await control.dispatchEvent('pointerup', { ...props, buttons: 0, clientX: x + 5, clientY: y });
  else await page.mouse.up();
  await page.waitForTimeout(150);
  return { startMs, endMs: await page.evaluate(() => performance.now()) };
}

for (const sensors of [1, 8, 32]) for (const mobile of [false, true]) {
  test(`chart production benchmark ${sensors} sensors ${mobile ? 'mobile4x' : 'desktop'}`, async ({ page, context, browser }, info) => {
    test.skip(!run || process.env.PM_BENCH_SOAK === '1' || extra, 'Opt-in production benchmark only.');
    await mkdir(directory, { recursive: true });
    await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 1000 });
    const cdp = await context.newCDPSession(page);
    await cdp.send('Emulation.setCPUThrottlingRate', { rate: mobile ? 4 : 1 });
    await cdp.send('Network.setCacheDisabled', { cacheDisabled: true });
    const installed = await install(page, sensors);
    const readyStarted = performance.now();
    await page.goto('/');
    await expect(page.getByTestId('usage-chart').locator('.recharts-curve').first()).toBeVisible();
    const readyMs = performance.now() - readyStarted;
    await page.waitForTimeout(1000);
    const before = await snapshot(page);
    const heartbeatStartRequests = installed.requests.length;
    for (let i = 0; i < 3; i += 1) { await emit(page, 'heartbeat'); await page.waitForTimeout(500); }
    const heartbeatRequests = installed.requests.length - heartbeatStartRequests;
    if (run?.startsWith('after')) expect(heartbeatRequests).toBe(0);
    const steadyStartRequests = installed.requests.length;
    for (let i = 0; i < 4; i += 1) { await emit(page, 'refresh'); await page.waitForTimeout(5000); }
    const steadyRequests = installed.requests.length - steadyStartRequests;
    if (run?.startsWith('after')) expect(steadyRequests).toBeLessThanOrEqual(2);
    const homeInteraction = await exercise(page, 'power', mobile);
    const selected = await page.getByTestId('power-selected-range').getAttribute('data-start-ms');
    await emit(page, 'measurement_accepted');
    await page.waitForTimeout(1200);
    expect(await page.getByTestId('power-selected-range').getAttribute('data-start-ms')).toBe(selected);
    await page.screenshot({ path: path.join(directory, `${sensors}-${mobile ? 'mobile' : 'desktop'}-home.png`), fullPage: true });
    await page.setViewportSize(mobile ? { width: 844, height: 390 } : { width: 1024, height: 768 });
    expect(await page.getByTestId('power-selected-range').getAttribute('data-start-ms')).toBe(selected);
    await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 1000 });
    const afterHome = await snapshot(page);
    await page.goto('/history');
    await expect(page.getByTestId('history-chart')).toBeVisible();
    const presets: Record<string, number> = {};
    for (const preset of ['Today', '7 days', '30 days', 'Billing cycle']) {
      const start = performance.now();
      await page.getByRole('button', { name: preset, exact: true }).click();
      await expect(page.getByTestId('history-chart').locator('.recharts-curve').first()).toBeVisible();
      presets[preset] = performance.now() - start;
    }
    const historyInteraction = await exercise(page, 'history', mobile);
    await page.screenshot({ path: path.join(directory, `${sensors}-${mobile ? 'mobile' : 'desktop'}-history.png`), fullPage: true });
    const afterHistory = await snapshot(page);
    const report = { run, sensors, profile: mobile ? '390x844 Chromium 4x CPU synthetic touch' : '1440x1000 Chromium 1x CPU mouse', browser: browser.version(), fixtureHomeId: homeScopes[0]!.id, networkDelayMs: 80, cache: 'disabled; new browser context', readyMs, heartbeatRequests, steadyRequests, homeRequests: installed.homeRequests(), requests: installed.requests, before, afterHome, afterHistory, presets, homeInteraction, historyInteraction,
      clock: 'Date calendar shifted to synthetic August13 and advances normally; performance/timers/RAF are unmodified native browser APIs',
      limitations: ['Fixture-backed normal API routes: no server SQL/ingestion/network performance claim.', 'Two native animation frames after changed range DOM are a local input-to-next-paint proxy, not field INP.', 'Mutation/resize counts instrument browser DOM, not React profiler commit counts.', 'No physical mobile device; touch PointerEvents are synthetic.'] };
    const filename = path.join(directory, `${sensors}-${mobile ? 'mobile' : 'desktop'}.json`);
    await writeFile(filename, JSON.stringify(report, null, 2));
    await info.attach('chart-performance', { path: filename, contentType: 'application/json' });
  });
}

test('ten-minute foreground resource soak with repeated production navigation', async ({ page, context }, info) => {
  test.skip(!run || process.env.PM_BENCH_SOAK !== '1', 'Opt-in ten-minute soak only.');
  test.setTimeout(720_000);
  await mkdir(directory, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await install(page, 32);
  const cdp = await context.newCDPSession(page);
  const samples: unknown[] = [];
  const start = performance.now();
  await page.goto('/');
  for (let iteration = 0; iteration <= 20; iteration += 1) {
    const target = start + iteration * 30_000;
    while (performance.now() < target) { await emit(page, 'refresh'); await page.waitForTimeout(Math.min(5000, target - performance.now())); }
    // Internal links retain the QueryClient: hard page reloads would hide cache/listener leaks.
    const route = iteration % 2 === 0 ? '/history' : '/';
    await page.locator(`a[href="${route}"]`).first().click();
    await expect(page.getByTestId(iteration % 2 === 0 ? 'history-chart' : 'usage-chart')).toBeVisible();
    await cdp.send('HeapProfiler.collectGarbage');
    const counters = await cdp.send('Memory.getDOMCounters');
    const heap = await cdp.send('Runtime.getHeapUsage');
    samples.push({ elapsedMs: performance.now() - start, route, counters, heap, snapshot: await snapshot(page) });
  }
  const file = path.join(directory, 'ten-minute-soak.json');
  await writeFile(file, JSON.stringify({ samples, durationMs: performance.now() - start, note: 'Forced GC at 30s samples; internal route navigation preserves application cache; SVG/resize/source/DOM/heap proxy evidence, not direct QueryClient introspection.' }, null, 2));
  await info.attach('soak', { path: file, contentType: 'application/json' });
});

for (const mobile of [false, true]) test(`bounded custom range and selectors ${mobile ? 'mobile4x' : 'desktop'}`, async ({ page, context }, info) => {
  test.skip(!run || !extra, 'Opt-in supplementary performance scenarios.');
  await mkdir(directory, { recursive: true });
  await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 1000 });
  const cdp = await context.newCDPSession(page);
  await cdp.send('Emulation.setCPUThrottlingRate', { rate: mobile ? 4 : 1 });
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: true });
  const installed = await install(page, 32);
  await page.goto('/history');
  await expect(page.getByTestId('history-chart')).toBeVisible();
  await page.getByRole('button', { name: 'Custom', exact: true }).click();
  await page.getByLabel('From', { exact: true }).fill('2026-08-03T10:32');
  await page.getByLabel('To', { exact: true }).fill('2026-08-13T10:32');
  const started = performance.now();
  const customResponse = page.waitForResponse((response) => response.url().includes('/api/v1/history?') && response.url().includes('from=2026-08-03'));
  await page.getByRole('button', { name: 'Apply range' }).click();
  await customResponse;
  await expect(page.getByTestId('history-chart').locator('.recharts-curve').first()).toBeVisible();
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  const customReadyMs = performance.now() - started;
  const interaction = await exercise(page, 'history', mobile, true);
  const curve = await page.getByTestId('history-chart').locator('.recharts-curve').first().getAttribute('d');
  expect((curve?.match(/M/g) ?? []).length).toBeGreaterThan(1);
  const selected = await page.getByTestId('history-selected-range').getAttribute('data-start-ms');
  await emit(page, 'measurement_accepted');
  await page.waitForTimeout(1200);
  expect(await page.getByTestId('history-selected-range').getAttribute('data-start-ms')).toBe(selected);
  const timings: Record<string, number> = {};
  const selection = async (label: string, value: string, key: string) => {
    const start = performance.now();
    // Returning to the just-loaded power scope may correctly reuse fresh cache.
    const response = value === 'power' ? null : page.waitForResponse((response) => response.url().includes('/api/v1/history?'));
    await page.getByLabel(label, { exact: true }).selectOption(value);
    if (response) await response;
    await expect(page.getByTestId('history-chart').locator('.recharts-curve').first()).toBeVisible();
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    timings[key] = performance.now() - start;
  };
  await selection('Service branch or sensor', 'device:device-0', 'sensor');
  for (const metric of ['voltage', 'current', 'frequency', 'power_factor', 'energy', 'cost', 'power']) await selection('Metric', metric, metric);
  const file = path.join(directory, `custom-${mobile ? 'mobile' : 'desktop'}.json`);
  await writeFile(file, JSON.stringify({ sensors: 32, points: 2880, range: '10 days / 300s', profile: mobile ? '390x844 4x CPU' : '1440x1000 1x CPU', customReadyMs, interaction, timings, requests: installed.requests, snapshot: await snapshot(page), note: 'Bounded full-result endpoint; this checkout has no continuation-pagination history API. Selector timing includes80ms fixture delay+UI rerender and is not continuous-input latency.' }, null, 2));
  await info.attach('custom-selector-performance', { path: file, contentType: 'application/json' });
  // Locator captures avoid sticky navigation obscuring a scrolled full-page image.
  await page.getByTestId('history-chart').screenshot({ path: path.join(directory, `custom-chart-${mobile ? 'mobile' : 'desktop'}.png`) });
  if (run?.startsWith('after')) {
    await page.goto('/');
    const pricing = page.getByRole('region', { name: 'Current electricity pricing' });
    await expect(pricing).toContainText('$0.172/kWh');
    await pricing.screenshot({ path: path.join(directory, `pricing-${mobile ? 'mobile' : 'desktop'}.png`) });
    await page.getByTestId('usage-chart').screenshot({ path: path.join(directory, `dashboard-chart-${mobile ? 'mobile' : 'desktop'}.png`) });
  }
});

test('separate React root commit attribution', async ({ page }, info) => {
  test.skip(!run || process.env.PM_BENCH_REACT !== '1', 'Opt-in attribution, separate from uninstrumented timing runs.');
  await mkdir(directory, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  const apiRequests: Record<string, number> = {};
  page.on('request', (request) => {
    const pathname = new URL(request.url()).pathname;
    if (pathname.startsWith('/api/v1/')) apiRequests[pathname] = (apiRequests[pathname] ?? 0) + 1;
  });
  const installed = await install(page, 32);
  await page.addInitScript(() => {
    const evidence = { commits: [] as number[], rendererVersions: [] as string[] };
    Object.assign(window, {
      reactCommitEvidence: evidence,
      __REACT_DEVTOOLS_GLOBAL_HOOK__: {
        supportsFiber: true,
        isDisabled: false,
        inject(renderer: { version?: string }) { evidence.rendererVersions.push(renderer.version ?? 'unknown'); return 1; },
        onCommitFiberRoot() { evidence.commits.push(performance.now()); },
      },
    });
  });
  await page.goto('/');
  await expect(page.getByTestId('usage-chart').locator('.recharts-curve').first()).toBeVisible();
  await page.waitForTimeout(1000);
  const before = await page.evaluate(() => (window as unknown as { reactCommitEvidence: { commits: number[]; rendererVersions: string[] } }).reactCommitEvidence);
  expect(before.rendererVersions.length).toBeGreaterThan(0);
  const historyBefore = installed.requests.length;
  const homeBefore = installed.homeRequests();
  const requestsBefore = { ...apiRequests };
  for (let i = 0; i < 4; i += 1) { await emit(page, 'refresh'); await page.waitForTimeout(5000); }
  const after = await page.evaluate(() => (window as unknown as { reactCommitEvidence: { commits: number[]; rendererVersions: string[] } }).reactCommitEvidence);
  const commits = after.commits.slice(before.commits.length);
  expect(commits.length).toBeGreaterThan(0);
  const file = path.join(directory, 'react-root-commits.json');
  await writeFile(file, JSON.stringify({ run, sensors: 32, viewport: '1440x1000', cpuThrottle: 1, genericRefreshEvents: 4, periodMs: 20_000,
    rendererVersions: after.rendererVersions, rootCommitCount: commits.length, rootCommitTimesMs: commits,
    historyRequests: installed.requests.length - historyBefore, homeRequests: installed.homeRequests() - homeBefore,
    apiRequestCounts: Object.fromEntries(Object.entries(apiRequests).map(([name, count]) => [name, count - (requestsBefore[name] ?? 0)]).filter(([, count]) => count !== 0)),
    note: 'Separate DevTools-hook attribution run. Counts actual production React onCommitFiberRoot notifications, including clock/fetch commits. Does not count individual component render invocations or measure React profiler durations; not used for primary native-frame latency.' }, null, 2));
  await info.attach('react-root-commits', { path: file, contentType: 'application/json' });
});
