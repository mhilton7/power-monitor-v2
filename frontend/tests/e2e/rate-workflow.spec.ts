import { expect, test } from '@playwright/test';
import { billing, homeScopes, rateCandidate } from '../fixtures';
import { mockApi } from './mocks';

test('official candidate advances through explicit review, publish and exact-account activation', async ({ page }) => {
  await mockApi(page);
  await page.goto('/billing');
  await page.getByText('Technical details').first().click();
  await expect(page.getByText('Last accepted rate information')).toBeVisible();
  await expect(page.getByText(`${rateCandidate.source.name} · official_https · ${rateCandidate.source.url}`)).toBeVisible();
  await expect(page.getByText('Last run source')).toBeVisible();

  const reviewRequest = page.waitForRequest((request) => request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/review'));
  await page.getByRole('button', { name: new RegExp(`Open official rate update ${rateCandidate.id}`) }).click();
  await page.getByLabel('Effective start date').fill('2026-08-01');
  await page.getByLabel(/confirmed this exact effective range/).check();
  await page.getByLabel(/confirmed the recorded source/).check();
  await page.getByRole('button', { name: 'Confirm candidate review' }).click();
  const reviewed = await reviewRequest;
  expect(new URL(reviewed.url()).searchParams.get('home_id')).toBe(homeScopes[0]!.id);
  expect(reviewed.postDataJSON()).toMatchObject({ selected_plan_name: 'TOU-D-4-9PM', administrator_confirmed_effective_date: true, administrator_confirmed_provenance: true });
  await expect(page.getByText(/Review recorded/)).toBeVisible();

  const publishRequest = page.waitForRequest((request) => request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/publish'));
  await page.getByRole('button', { name: 'Publish reviewed version' }).click();
  await page.getByRole('button', { name: 'Publish immutable version' }).click();
  expect(new URL((await publishRequest).url()).searchParams.get('home_id')).toBe(homeScopes[0]!.id);
  await expect(page.getByText(/Immutable version published/)).toBeVisible();

  const activateRequest = page.waitForRequest((request) => request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/activate'));
  await page.getByRole('button', { name: 'Activate for selected account' }).click();
  await page.getByRole('button', { name: 'Activate exact account' }).click();
  const activated = await activateRequest;
  expect(new URL(activated.url()).searchParams.get('home_id')).toBe(homeScopes[0]!.id);
  expect(activated.postDataJSON()).toEqual({ utility_account_id: billing.accounts[0]!.utility_account_id });
  await expect(page.getByText(new RegExp(`Active for account ${billing.accounts[0]!.utility_account_id}`))).toBeVisible();
});

test('candidate rejection confirms, exposes loading and error, resets, then becomes terminal', async ({ page }) => {
  const requests: Array<{ url: URL; body: string | null }> = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.pathname.endsWith('/reject')) requests.push({ url, body: request.postData() });
  });
  await mockApi(page, { rateRejectFailureOnce: true, rateRejectDelayMs: 250 });
  await page.goto('/billing');
  await page.getByRole('button', { name: new RegExp(`Open official rate update ${rateCandidate.id}`) }).click();

  await page.getByRole('button', { name: 'Reject candidate' }).click();
  await expect(page.getByRole('dialog', { name: 'Reject this rate candidate?' })).toBeVisible();
  await page.getByRole('button', { name: 'Reject candidate permanently' }).click();
  await expect(page.getByRole('button', { name: 'Working…' })).toBeVisible();
  await expect(page.getByText(/only an unpublished candidate can be rejected/)).toBeVisible();

  await page.getByRole('button', { name: 'Reject candidate' }).click();
  await expect(page.getByText(/only an unpublished candidate can be rejected/)).toHaveCount(0);
  await page.getByRole('button', { name: 'Reject candidate permanently' }).click();
  await expect(page.getByRole('button', { name: 'Working…' })).toBeVisible();
  await expect(page.getByText(/This candidate was rejected and cannot advance/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Reject candidate' })).toHaveCount(0);
  expect(requests).toHaveLength(2);
  for (const request of requests) {
    expect(request.url.searchParams.get('home_id')).toBe(homeScopes[0]!.id);
    expect(request.body).toBeNull();
  }
});

test('synchronous failed source check is never described as queued or successful', async ({ page }) => {
  await mockApi(page, { rateCheckOverride: { run_id: 'run-failed', state: 'failed', event_code: 'RATE_SOURCE_VALIDATION_FAILED', revision_id: null, candidate_id: null, error_code: 'HOLIDAY_RULE_MISSING' } });
  await page.goto('/billing');
  await page.getByRole('button', { name: 'Check now' }).click();
  await expect(page.getByText(/HOLIDAY_RULE_MISSING/)).toBeVisible();
  await expect(page.getByText(/No candidate or active rate was changed/)).toBeVisible();
  await expect(page.getByText(/queued/i)).toHaveCount(0);
});

test('manual fallback sends only rate facts and remains review-required', async ({ page }) => {
  await mockApi(page);
  await page.goto('/billing');
  await page.getByRole('button', { name: 'Enter rates manually' }).click();
  await page.getByLabel('Official source title').fill('SCE Schedule D official tariff');
  await page.getByLabel('Tariff identifier').fill('Schedule D 2026-08-01');
  await page.getByLabel('Official SCE HTTPS URL (optional)').fill('https://www.sce.com/regulatory/tariff-books/rates-pricing-choices');
  await page.getByLabel('Rate plan name').fill('MANUAL-TOU-D');
  await page.getByLabel('Effective start date').fill('2026-08-01');
  await page.getByLabel('USD per kWh').fill('0.12345678');
  await page.getByLabel(/I attest these reusable rate facts/).check();
  const requestPromise = page.waitForRequest((request) => request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/manual-candidates'));
  await page.getByRole('button', { name: 'Create review-required candidate' }).click();
  const request = await requestPromise;
  expect(new URL(request.url()).searchParams.get('home_id')).toBe(homeScopes[0]!.id);
  const payload = request.postDataJSON() as Record<string, unknown>;
  expect(payload).toMatchObject({ administrator_attests_official_source: true, rate_plan_name: 'MANUAL-TOU-D', periods: [{ season: 'all', day_type: 'all', start_minute: 0, end_minute: 1440, price_per_kwh: '0.12345678' }] });
  expect(Object.keys(payload)).not.toEqual(expect.arrayContaining(['customer_name', 'account_number', 'usage_kwh', 'meter_reading', 'amount_due']));
  await expect(page.getByText(/Manual candidate created for this home/)).toBeVisible();
  await expect(page.getByRole('dialog', { name: 'Review SCE rate update' })).toBeVisible();
  await expect(page.getByText('Manual official-source entry')).toBeVisible();
});

test('multi-home selection scopes candidate reads and clears prior-home review state', async ({ page }) => {
  const firstHome = homeScopes[0]!.id;
  const secondHome = '00000000-0000-0000-0000-000000000011';
  const scopes = [{ id: firstHome, name: 'Home' }, { id: secondHome, name: 'Home' }];
  const secondCandidate = structuredClone(rateCandidate);
  secondCandidate.id = '00000000-0000-0000-0000-000000000080';
  secondCandidate.normalized_rates.plans[0]!.rate_plan_name = 'SECOND-HOME-TOU';
  const candidateRequests: string[] = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.pathname.endsWith('/rate-sources/candidates')) candidateRequests.push(url.searchParams.get('home_id') ?? '');
  });
  await mockApi(page, {
    homeScopesOverride: scopes,
    rateCandidatesById: {
      [firstHome]: { home_id: firstHome, candidates: [rateCandidate] },
      [secondHome]: { home_id: secondHome, candidates: [secondCandidate] },
    },
  });
  await page.goto('/billing');
  const selector = page.getByLabel('Active home');
  await selector.selectOption(firstHome);
  await page.getByRole('button', { name: new RegExp(`Open official rate update ${rateCandidate.id}`) }).click();
  await expect(page.getByRole('dialog', { name: 'Review SCE rate update' })).toBeVisible();

  await selector.selectOption(secondHome);
  await expect(page.getByRole('dialog', { name: 'Review SCE rate update' })).toHaveCount(0);
  await expect(page.getByText('SECOND-HOME-TOU')).toBeVisible();
  await expect(page.getByText('TOU-D-4-9PM', { exact: true })).toHaveCount(0);
  expect(candidateRequests).toContain(firstHome);
  expect(candidateRequests).toContain(secondHome);
  expect(candidateRequests.every((value) => value === firstHome || value === secondHome)).toBe(true);
});
