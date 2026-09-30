import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: /chart-performance\.spec\.ts/,
  timeout: 180_000,
  workers: 1,
  retries: 0,
  reporter: 'list',
  outputDir: `../../../.test-runtime/chart-performance/${process.env.PM_CHART_BENCHMARK ?? 'disabled'}/playwright-${process.env.PM_BENCH_REACT === '1' ? 'react' : process.env.PM_BENCH_EXTRA === '1' ? 'extra' : process.env.PM_BENCH_SOAK === '1' ? 'soak' : 'matrix'}`,
  use: {
    baseURL: 'http://127.0.0.1:4181',
    locale: 'en-US',
    timezoneId: 'America/Los_Angeles',
    trace: process.env.PM_BENCH_SOAK === '1' ? 'off' : 'on',
  },
  webServer: {
    command: 'node tests/e2e/chart-performance-server.ts',
    cwd: '../..',
    url: 'http://127.0.0.1:4181',
    reuseExistingServer: false,
  },
});
