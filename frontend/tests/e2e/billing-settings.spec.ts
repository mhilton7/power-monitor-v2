import { expect, test } from '@playwright/test';
import { session } from '../fixtures';
import { mockApi } from './mocks';

test.beforeEach(async ({ page }) => { await mockApi(page); });

test('rate-only PDF review excludes prohibited bill usage and comparison surfaces', async ({ page }) => {
  await page.goto('/billing');
  await page.getByRole('button', { name: 'Import rates from SCE bill PDF' }).click();
  await expect(page.getByRole('heading', { name: 'Rates and reusable cost rules only' })).toBeVisible();
  await expect(page.getByText(/No upload can create or change sensor readings/)).toBeVisible();
  const body = await page.locator('body').innerText();
  expect(body).not.toMatch(/actual[- ]versus[- ]bill/i);
  expect(body).not.toMatch(/historical bill comparison/i);
  expect(body).not.toMatch(/amount due:\s*\$/i);
});

test('rate-source Check now and backup/system health evidence work', async ({ page }) => {
  await page.goto('/billing');
  await page.getByRole('button', { name: 'Check now' }).click();
  await expect(page.getByText(/Sync run rate-run-1 queued/)).toBeVisible();
  await page.goto('/settings');
  await page.getByRole('button', { name: /Backups & restore/ }).click();
  await expect(page.getByText('Backup checksum')).toBeVisible();
  await expect(page.getByText('Last isolated restore test')).toBeVisible();
  await page.getByRole('button', { name: /Advanced system health/ }).click();
  await expect(page.getByText('reachable')).toBeVisible();
});

test('user permission changes are available only on the scoped settings surface', async ({ page }) => {
  await page.goto('/settings');
  await page.getByRole('button', { name: /Users & access/ }).click();
  await page.getByRole('button', { name: /Alex Morgan/ }).click();
  await expect(page.getByRole('dialog', { name: /Access for Alex Morgan/ })).toBeVisible();
  await page.getByRole('checkbox', { name: /^Owner\d+ permissions$/ }).uncheck();
  await page.getByRole('checkbox', { name: /^Owner\d+ permissions$/ }).check();
  await page.getByRole('button', { name: 'Save access' }).click();
  await expect(page.getByRole('dialog', { name: /Access for Alex Morgan/ })).not.toBeVisible();
});

test('session expiry returns to login without retaining protected views', async ({ page }) => {
  await page.unrouteAll({ behavior: 'wait' });
  await mockApi(page, { sessionExpired: true });
  await page.goto('/settings');
  await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Settings' })).not.toBeVisible();
});

test('sensor enrollment, configuration and signed firmware surfaces use concrete server routes', async ({ page }) => {
  await page.goto('/settings');
  await page.getByRole('button', { name: 'Sensors' }).click();
  await expect(page.getByRole('button', { name: /Main Panel Sensor/ })).toBeVisible();
  await page.getByRole('button', { name: 'Enroll sensor' }).click();
  await expect(page.getByRole('heading', { name: 'Create one-time sensor enrollment' })).toBeVisible();
  await expect(page.getByText(/New one-CT sensors start in energy-only scope/)).toBeVisible();
  await page.getByRole('button', { name: 'Cancel' }).click();

  await page.getByRole('button', { name: /Main Panel Sensor/ }).click();
  await page.getByRole('button', { name: 'Configure sensor' }).click();
  await page.getByLabel('Friendly name').fill('Main Panel Sensor');
  const updateRequest = page.waitForRequest((request) => request.method() === 'PATCH' && request.url().endsWith('/api/v1/devices/device-main'));
  await page.getByRole('button', { name: 'Save sensor' }).click();
  await updateRequest;
  await page.getByRole('button', { name: 'Close Main Panel Sensor' }).click();

  await page.getByRole('button', { name: 'Firmware' }).click();
  await expect(page.getByText(/1\.2\.4 · build 851/)).toBeVisible();
  await expect(page.getByText(/physical certification pending/)).toBeVisible();
  await page.getByRole('button', { name: 'Deploy' }).click();
  const deployRequest = page.waitForRequest((request) => request.method() === 'POST' && request.url().endsWith('/deploy'));
  await page.getByRole('button', { name: 'Queue deployment' }).click();
  await deployRequest;
});

test('first sensor enrollment uses its authorized home without billing access or an existing device', async ({ page }) => {
  const homeId = '00000000-0000-0000-0000-000000000010';
  await page.unrouteAll({ behavior: 'wait' });
  await mockApi(page, {
    devicesOverride: [],
    homeScopesOverride: [{ id: homeId, name: 'Home' }],
    sessionOverride: { ...session.user, roles: ['Sensor installer'], permissions: ['sensors.view', 'sensors.enroll'] },
  });
  await page.goto('/settings');
  await expect(page.getByText(/No sensors are enrolled yet/)).toBeVisible();
  await page.getByRole('button', { name: 'Enroll sensor' }).click();
  await expect(page.getByText('Enrollment home: Home')).toBeVisible();
  await page.getByLabel('Friendly name').fill('Main panel');
  const requestPromise = page.waitForRequest((request) => request.method() === 'POST' && request.url().endsWith('/api/v1/enrollment-tokens'));
  await page.getByRole('button', { name: 'Create token' }).click();
  const request = await requestPromise;
  expect(request.postDataJSON()).toMatchObject({ home_id: homeId, friendly_name: 'Main panel' });
  await expect(page.getByText('single-use-enrollment-token-value-000000000000')).toBeVisible();
});
