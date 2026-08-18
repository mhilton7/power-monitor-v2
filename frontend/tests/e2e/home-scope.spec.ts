import { expect, test } from '@playwright/test';
import { billing, device, home, homeUtility } from '../fixtures';
import { mockApi } from './mocks';

const firstHomeId = '00000000-0000-0000-0000-000000000010';
const secondHomeId = '00000000-0000-0000-0000-000000000011';
const duplicateHomeScopes = [{ id: firstHomeId, name: 'Home' }, { id: secondHomeId, name: 'Home' }];

test('requires a UUID-disambiguated home and binds every home-specific page request', async ({ page }) => {
  const featureRequests: URL[] = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (request.method() === 'GET' && ['/api/v1/home', '/api/v1/history', '/api/v1/history/export.csv', '/api/v1/billing', '/api/v1/bill-rate-imports', '/api/v1/devices', '/api/v1/circuits', '/api/v1/settings/home-utility'].includes(url.pathname)) featureRequests.push(url);
  });
  await mockApi(page, { homeScopesOverride: duplicateHomeScopes });
  await page.goto('/');

  await expect(page.getByRole('heading', { name: 'Choose an active home' })).toBeVisible();
  await page.waitForTimeout(100);
  expect(featureRequests).toEqual([]);
  const selector = page.getByLabel('Active home');
  await expect(selector.getByRole('option', { name: 'Home (1)' })).toHaveAttribute('value', firstHomeId);
  await expect(selector.getByRole('option', { name: 'Home (2)' })).toHaveAttribute('value', secondHomeId);
  await selector.selectOption(firstHomeId);
  await expect(page.getByRole('heading', { name: 'Live Power Usage' })).toBeVisible();
  await expect(page).toHaveTitle('PowerMeter V2');

  await page.locator('.sidebar').getByRole('link', { name: 'History' }).click();
  await expect(page.getByTestId('history-chart')).toBeVisible();
  await expect(page.locator('#main-content')).toBeFocused();
  await expect(page).toHaveTitle('History · PowerMeter V2');
  const exportRequest = page.waitForRequest((request) => new URL(request.url()).pathname === '/api/v1/history/export.csv');
  await page.getByRole('button', { name: 'Export CSV' }).click();
  await exportRequest;

  await page.locator('.sidebar').getByRole('link', { name: 'Billing' }).click();
  await expect(page.getByRole('heading', { name: 'Billing', exact: true })).toBeVisible();
  await expect(page).toHaveTitle('Billing · PowerMeter V2');
  await page.getByRole('button', { name: 'Import rates from SCE bill PDF' }).click();
  await page.getByLabel('Choose an SCE PDF rate source').setInputFiles({ name: 'rates.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.7') });
  const uploadRequest = page.waitForRequest((request) => request.method() === 'POST' && new URL(request.url()).pathname === '/api/v1/bill-rate-imports');
  await page.getByRole('button', { name: 'Create rate draft' }).click();
  const uploaded = await uploadRequest;
  expect(uploaded.postDataBuffer()?.toString('utf8')).toContain(firstHomeId);
  await page.getByRole('button', { name: 'Close Import rates from SCE bill PDF' }).click();
  await page.locator('.sidebar').getByRole('link', { name: 'Settings' }).click();
  await expect(page.getByLabel('Home name')).toBeVisible();
  await expect(page).toHaveTitle('Settings · PowerMeter V2');

  for (const path of ['/api/v1/home', '/api/v1/history', '/api/v1/history/export.csv', '/api/v1/billing', '/api/v1/bill-rate-imports', '/api/v1/devices', '/api/v1/circuits', '/api/v1/settings/home-utility']) {
    expect(featureRequests.some((url) => url.pathname === path)).toBe(true);
  }
  for (const request of featureRequests) expect(request.searchParams.get('home_id')).toBe(firstHomeId);
});

test('removes the prior home immediately while a newly selected home is loading', async ({ page }) => {
  const firstDevice = { ...device, id: 'device-first', home_id: firstHomeId, friendly_name: 'First home sensor' };
  const secondDevice = { ...device, id: 'device-second', home_id: secondHomeId, friendly_name: 'Second home sensor' };
  await mockApi(page, {
    homeScopesOverride: duplicateHomeScopes,
    homeById: {
      [firstHomeId]: { ...home, devices: [{ ...home.devices[0]!, id: firstDevice.id, friendly_name: firstDevice.friendly_name }] },
      [secondHomeId]: { ...home, devices: [{ ...home.devices[0]!, id: secondDevice.id, friendly_name: secondDevice.friendly_name }] },
    },
    devicesById: {
      [firstHomeId]: { home_scopes: duplicateHomeScopes, devices: [firstDevice] },
      [secondHomeId]: { home_scopes: duplicateHomeScopes, devices: [secondDevice] },
    },
    billingById: {
      [firstHomeId]: { ...billing, accounts: [{ ...billing.accounts[0]!, plan_name: 'First home rate' }] },
      [secondHomeId]: { ...billing, accounts: [{ ...billing.accounts[0]!, plan_name: 'Second home rate' }] },
    },
    homeUtilityById: {
      [firstHomeId]: { ...homeUtility, home: { ...homeUtility.home, id: firstHomeId, name: 'First home' } },
      [secondHomeId]: { ...homeUtility, home: { ...homeUtility.home, id: secondHomeId, name: 'Second home' } },
    },
    delayedHomeId: secondHomeId,
    delayMs: 800,
  });
  await page.goto('/');
  const selector = page.getByLabel('Active home');
  await selector.selectOption(firstHomeId);
  await expect(page.getByText('First home sensor', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: /Sync Now/ }).click();
  await expect(page.getByText(/Command .* is queued/)).toBeVisible();

  await selector.selectOption(secondHomeId);
  await expect(page.getByText('First home sensor', { exact: true })).toHaveCount(0);
  await expect(page.getByText(/Command .* is queued/)).toHaveCount(0);
  await expect(page.getByText('Loading authenticated measurements')).toBeVisible();
  await expect(page.getByText('Second home sensor', { exact: true }).first()).toBeVisible();
  await expect(selector).toHaveValue(secondHomeId);
});
