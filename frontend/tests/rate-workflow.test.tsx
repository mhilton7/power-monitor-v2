import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { api } from '../src/api';
import { BillingPage } from '../src/pages/BillingPage';
import { RateSourceWorkflow } from '../src/rates/RateSourceWorkflow';
import { rateCandidateSchema } from '../src/api/schemas';
import { apiResponse, billing, homeScopes, rateCandidate, rateCandidates, sceRateCatalog } from './fixtures';
import { installFetchMock, renderWithProviders } from './render';

const homeId = homeScopes[0]!.id;

function jsonRequestBody(body: BodyInit | null | undefined): Record<string, unknown> {
  if (typeof body !== 'string') throw new Error('Expected a JSON request body.');
  return JSON.parse(body) as Record<string, unknown>;
}

function WorkflowSwitchHarness({ secondHomeId }: { secondHomeId: string }) {
  const [activeHomeId, setActiveHomeId] = useState(homeId);
  return <><button type="button" onClick={() => setActiveHomeId(secondHomeId)}>Switch test home</button><RateSourceWorkflow key={activeHomeId} homeId={activeHomeId} accounts={billing.accounts} /></>;
}

describe('exact-home SCE rate workflow', () => {
  it('renders only the four structurally discovered residential plan families', async () => {
    const catalog = structuredClone(sceRateCatalog);
    const base = catalog.plans[0]!;
    catalog.plans = [
      base,
      { ...structuredClone(base), id: 'catalog-tou-4-9', canonical_name: 'TOU-D-4-9PM', public_plan_name: 'TOU-D 4 PM to 9 PM', official_schedule_code: 'TOU-D-4-9PM', plan_type: 'time_of_use_with_baseline_credit', currently_used: false },
      { ...structuredClone(base), id: 'catalog-tou-5-8', canonical_name: 'TOU-D-5-8PM', public_plan_name: 'TOU-D 5 PM to 8 PM', official_schedule_code: 'TOU-D-5-8PM', plan_type: 'time_of_use_with_baseline_credit', currently_used: false },
      { ...structuredClone(base), id: 'catalog-tou-prime', canonical_name: 'TOU-D-PRIME', public_plan_name: 'TOU-D-PRIME', official_schedule_code: 'TOU-D-PRIME', plan_type: 'time_of_use', currently_used: false },
    ];
    catalog.summary.plans_discovered = 4;
    catalog.summary.plans_parsed = 4;
    installFetchMock((path, method) => path.includes('/rate-sources/catalog')
      ? { status: 200, body: catalog }
      : apiResponse(path, method));
    renderWithProviders(<RateSourceWorkflow homeId={homeId} accounts={billing.accounts} />);

    for (const name of ['SCE Domestic', 'TOU-D 4 PM to 9 PM', 'TOU-D 5 PM to 8 PM', 'TOU-D-PRIME']) {
      expect(await screen.findByRole('button', { name: `View ${name} rate plan` })).toBeInTheDocument();
    }
    expect(screen.queryByText(/Solar Billing Plan/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Understanding Updates to Your Electricity Bill/)).not.toBeInTheDocument();
  });

  it('labels the bounded official catalog incomplete and keeps technical source values behind details', async () => {
    installFetchMock(apiResponse);
    renderWithProviders(<RateSourceWorkflow homeId={homeId} accounts={billing.accounts} />);

    expect(await screen.findByRole('heading', { name: 'Available SCE rate plans' })).toBeInTheDocument();
    expect(await screen.findByText(/Silently omitted plans: unknown\./)).toBeInTheDocument();
    expect(screen.getByText(/has not yet accounted for every discovered in-scope document/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'View SCE Domestic rate plan' }));

    const dialog = screen.getByRole('dialog', { name: 'SCE Domestic' });
    expect(within(dialog).getByText('$0.76900 per day')).toBeInTheDocument();
    expect(within(dialog).getAllByText('Summer')).toHaveLength(3);
    expect(within(dialog).getByText('$0.30863/kWh')).toBeInTheDocument();
    expect(within(dialog).getByText('sce-public-catalog-v1')).not.toBeVisible();
    await userEvent.click(within(dialog).getByText('Technical details'));
    expect(within(dialog).getByText('sce-public-catalog-v1')).toBeInTheDocument();
  });

  it('distinguishes proven discovery closure from reviewed rate approval', async () => {
    const closedCatalog = {
      ...structuredClone(sceRateCatalog),
      catalog_completeness: 'closure_proved',
      catalog_ready: true,
      completeness_reason: 'all_discovered_links_accounted_for',
      summary: { ...sceRateCatalog.summary, plans_silently_omitted: 0 },
    };
    installFetchMock((path, method) => path.includes('/rate-sources/catalog')
      ? { status: 200, body: closedCatalog }
      : apiResponse(path, method));
    renderWithProviders(<RateSourceWorkflow homeId={homeId} accounts={billing.accounts} />);

    expect(await screen.findByText(/No plan link was silently omitted/)).toBeInTheDocument();
    expect(screen.getByText(/Exact rates, eligibility, and home-specific parameters still require/)).toBeInTheDocument();
  });

  it('shows an unsplit combined rate once instead of duplicating it as delivery and generation', async () => {
    const combinedCatalog = structuredClone(sceRateCatalog);
    combinedCatalog.plans[0]!.periods[0]!.rate_components = [{ component: 'sce_delivery_and_generation_combined', amount_per_kwh: '0.30863' }];
    installFetchMock((path, method) => path.includes('/rate-sources/catalog')
      ? { status: 200, body: combinedCatalog }
      : apiResponse(path, method));
    renderWithProviders(<RateSourceWorkflow homeId={homeId} accounts={billing.accounts} />);

    await userEvent.click(await screen.findByRole('button', { name: 'View SCE Domestic rate plan' }));
    const dialog = screen.getByRole('dialog', { name: 'SCE Domestic' });
    const row = within(dialog).getAllByText('Tier 1')[0]!.closest('tr');
    expect(row).not.toBeNull();
    const cells = within(row!).getAllByRole('cell');
    expect(cells[4]).toHaveTextContent('—');
    expect(cells[5]).toHaveTextContent('—');
    expect(cells[6]).toHaveTextContent('$0.30863');
  });

  it('labels consumer-page prices as rounded instead of claiming exact tariff totals', async () => {
    const roundedCatalog = structuredClone(sceRateCatalog);
    roundedCatalog.plans[0]!.rate_precision = 'consumer_display_rounded';
    roundedCatalog.plans[0]!.exact_rates_verified = false;
    installFetchMock((path, method) => path.includes('/rate-sources/catalog')
      ? { status: 200, body: roundedCatalog }
      : apiResponse(path, method));
    renderWithProviders(<RateSourceWorkflow homeId={homeId} accounts={billing.accounts} />);

    await userEvent.click(await screen.findByRole('button', { name: 'View SCE Domestic rate plan' }));
    const dialog = screen.getByRole('dialog', { name: 'SCE Domestic' });
    expect(within(dialog).getByText('Rounded public prices.')).toBeInTheDocument();
    expect(within(dialog).getByRole('columnheader', { name: 'Rounded public total' })).toBeInTheDocument();
    expect(within(dialog).queryByRole('columnheader', { name: 'Exact total' })).not.toBeInTheDocument();
  });

  it('rejects prohibited or uncontracted fields in normalized candidate rate facts', () => {
    expect(() => rateCandidateSchema.parse({
      ...rateCandidate,
      normalized_rates: { ...rateCandidate.normalized_rates, usage_kwh: '999.0' },
    })).toThrow();
  });

  it('accepts the dated rounded-price tiered candidate returned by the official SCE parser', () => {
    const normalized = {
      ...rateCandidate.normalized_rates,
      plan_classification: 'seasonal_tiered',
      holiday_treatment: 'not_applicable',
      holiday_rule: 'not_applicable',
      effective_start: '2026-08-01',
      effective_date_confirmation_required: false,
      plans: [{
        ...rateCandidate.normalized_rates.plans[0],
        pricing_model: 'seasonal_tiered',
        rate_precision: 'consumer_display_rounded',
        tier_threshold_basis: 'home_baseline_allocation_review_required',
      }],
    };
    const parsed = rateCandidateSchema.parse({
      ...rateCandidate,
      normalized_rates: normalized,
      validation_evidence: {
        ...rateCandidate.validation_evidence,
        plan_classification: 'seasonal_tiered',
        holiday_treatment: 'not_applicable',
        day_types: ['all'],
        coverage: 'semantic_tier_coverage',
        effective_date: '2026-08-01',
        warnings: ['HOME_BASELINE_ALLOCATION_REVIEW_REQUIRED'],
        source_artifact_sha256: rateCandidate.source.artifact_sha256,
        source_revision_id: rateCandidate.source.revision_id,
      },
      diff: {
        schema: 'sce-rate-diff/1.0.0',
        previous_candidate_id: null,
        before: normalized,
        after: normalized,
        changes: [],
        change_count: 0,
        truncated: false,
      },
    });
    expect(parsed.normalized_rates.effective_start).toBe('2026-08-01');
    expect(parsed.normalized_rates.effective_date_confirmation_required).toBe(false);
    expect(parsed.normalized_rates.plans[0]?.rate_precision).toBe('consumer_display_rounded');
    expect(parsed.diff.after?.effective_date_confirmation_required).toBe(false);
    expect(parsed.diff.after?.plans[0]?.rate_precision).toBe('consumer_display_rounded');
  });

  it('shows semantic tier evidence without allowing an incomplete baseline to advance', async () => {
    const candidate = {
      ...rateCandidate,
      normalized_rates: {
        ...rateCandidate.normalized_rates,
        plan_classification: 'seasonal_tiered',
        holiday_treatment: 'not_applicable',
        holiday_rule: 'not_applicable',
        effective_start: '2026-08-01',
        plans: [{
          ...rateCandidate.normalized_rates.plans[0],
          rate_plan_name: 'DOMESTIC',
          pricing_model: 'seasonal_tiered',
          tier_threshold_basis: 'home_baseline_allocation_review_required',
        }],
      },
      validation_evidence: {
        ...rateCandidate.validation_evidence,
        day_types: ['all'],
        coverage: 'semantic_tier_coverage',
        effective_date: '2026-08-01',
      },
    };
    installFetchMock((path, method) => {
      const url = new URL(path, 'http://frontend.test');
      if (url.pathname.endsWith('/rate-sources/candidates') && method === 'GET') {
        return { status: 200, body: { ...rateCandidates, candidates: [candidate] } };
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<BillingPage mode="settings" />);

    await userEvent.click(await screen.findByRole('button', { name: new RegExp(`Open official rate update ${candidate.id}`) }));
    expect(screen.getByText(/Additional baseline evidence required/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Apply reviewed rate plan' })).toBeDisabled();
  });

  it('rejects a typed rate response whose home UUID does not match the request', async () => {
    installFetchMock((path, method) => {
      const url = new URL(path, 'http://frontend.test');
      if (url.pathname.endsWith('/rate-sources/candidates')) return { status: 200, body: { ...rateCandidates, home_id: '00000000-0000-0000-0000-000000000011' } };
      return apiResponse(path, method);
    });
    await expect(api.rateSourceCandidates(homeId)).rejects.toThrow('different home');
  });

  it('reports a synchronous validation failure honestly and preserves exact-home scope', async () => {
    let requestedHome = '';
    installFetchMock((path, method) => {
      const url = new URL(path, 'http://frontend.test');
      if (url.pathname.endsWith('/rate-sources/check-now') && method === 'POST') {
        requestedHome = url.searchParams.get('home_id') ?? '';
        return { status: 202, body: { run_id: 'run-failed', state: 'failed', event_code: 'RATE_SOURCE_VALIDATION_FAILED', revision_id: null, candidate_id: null, error_code: 'HOLIDAY_RULE_MISSING' } };
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<BillingPage mode="settings" />);

    expect(await screen.findByText(`${rateCandidate.source.name} · official_https · ${rateCandidate.source.url}`)).toBeInTheDocument();
    await userEvent.click(await screen.findByRole('button', { name: 'Check now' }));
    expect(await screen.findByText(/HOLIDAY_RULE_MISSING/)).toBeInTheDocument();
    expect(screen.getByText(/No candidate or active rate was changed/)).toBeInTheDocument();
    expect(screen.queryByText(/queued/i)).not.toBeInTheDocument();
    expect(requestedHome).toBe(homeId);
  });

  it('reports a valid unchanged tiered check as success without a holiday error', async () => {
    installFetchMock((path, method) => {
      const url = new URL(path, 'http://frontend.test');
      if (url.pathname.endsWith('/rate-sources/check-now') && method === 'POST') {
        return { status: 202, body: { run_id: 'run-unchanged', state: 'unchanged', event_code: 'RATE_SOURCE_CONTENT_UNCHANGED', revision_id: rateCandidate.source.revision_id, candidate_id: rateCandidate.id, error_code: null } };
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<BillingPage mode="settings" />);

    await userEvent.click(await screen.findByRole('button', { name: 'Check now' }));
    expect(await screen.findByText(/The verified source is unchanged/)).toBeInTheDocument();
    expect(screen.queryByText(/HOLIDAY_RULE_MISSING/)).not.toBeInTheDocument();
    expect(screen.queryByText(/completed with a failure/)).not.toBeInTheDocument();
  });

  it('requires review confirmations before separate publish and exact-account activation', async () => {
    let workflow: Record<string, unknown> = { state: 'review_required' };
    const requests: Array<{ path: string; body: Record<string, unknown> }> = [];
    installFetchMock((path, method, body) => {
      const url = new URL(path, 'http://frontend.test');
      if (url.pathname.endsWith('/rate-sources/candidates') && method === 'GET') return { status: 200, body: { ...rateCandidates, home_id: homeId, candidates: [{ ...rateCandidate, workflow }] } };
      if (url.pathname.endsWith('/review') && method === 'POST') {
        const payload = jsonRequestBody(body);
        requests.push({ path, body: payload });
        workflow = { id: 'review-1', state: 'reviewed', selected_plan_name: payload.selected_plan_name, effective_start: payload.effective_start, effective_end: null, reviewed_at: '2026-08-13T17:00:00Z', published_at: null, activated_at: null, rate_plan_version_id: null, utility_account_id: null };
        return { status: 200, body: { home_id: homeId, candidate_id: rateCandidate.id, workflow } };
      }
      if (url.pathname.endsWith('/publish') && method === 'POST') {
        requests.push({ path, body: {} });
        workflow = { ...workflow, state: 'published', published_at: '2026-08-13T17:01:00Z', rate_plan_version_id: 'rate-version-new' };
        return { status: 201, body: { home_id: homeId, candidate_id: rateCandidate.id, workflow, rate_plan_version: { id: 'rate-version-new', plan_id: 'rate-plan-1', plan_name: 'TOU-D-4-9PM', version: 2, effective_start: workflow.effective_start, effective_end: null, source_artifact_sha256: rateCandidate.source.artifact_sha256, state: 'published' } } };
      }
      if (url.pathname.endsWith('/activate') && method === 'POST') {
        const payload = jsonRequestBody(body);
        requests.push({ path, body: payload });
        workflow = { ...workflow, state: 'activated', activated_at: '2026-08-13T17:02:00Z', utility_account_id: payload.utility_account_id };
        return { status: 201, body: { home_id: homeId, candidate_id: rateCandidate.id, workflow, assignment: { id: 'assignment-new', utility_account_id: payload.utility_account_id, rate_plan_version_id: 'rate-version-new', effective_start: workflow.effective_start, effective_end: null } } };
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<BillingPage mode="settings" />);

    await userEvent.click(await screen.findByRole('button', { name: new RegExp(`Open official rate update ${rateCandidate.id}`) }));
    await userEvent.type(screen.getByLabelText('Effective start date'), '2026-08-01');
    await userEvent.click(screen.getByLabelText(/confirmed this exact effective range/));
    await userEvent.click(screen.getByLabelText(/confirmed the recorded source/));
    await userEvent.click(screen.getByRole('button', { name: 'Apply reviewed rate plan' }));
    expect(await screen.findByText(/Review recorded/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Publish reviewed version' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Publish immutable version' }));
    expect(await screen.findByText(/Immutable version published/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Activate for selected account' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Activate exact account' }));
    expect(await screen.findByText(/Active for the selected account/)).toBeInTheDocument();

    expect(requests).toHaveLength(3);
    for (const request of requests) expect(new URL(request.path, 'http://frontend.test').searchParams.get('home_id')).toBe(homeId);
    expect(requests[0]!.body).toMatchObject({ selected_plan_name: 'TOU-D-4-9PM', effective_start: '2026-08-01T07:00:00.000Z', administrator_confirmed_effective_date: true, administrator_confirmed_provenance: true });
    expect(requests[2]!.body).toEqual({ utility_account_id: billing.accounts[0]!.utility_account_id });
  });

  it('confirms rejection, surfaces a server conflict, resets the error, and then becomes terminal', async () => {
    let attempts = 0;
    let workflow: Record<string, unknown> = { state: 'review_required' };
    const requestBodies: Array<BodyInit | null | undefined> = [];
    installFetchMock((path, method, body) => {
      const url = new URL(path, 'http://frontend.test');
      if (url.pathname.endsWith('/rate-sources/candidates') && method === 'GET') return { status: 200, body: { ...rateCandidates, candidates: [{ ...rateCandidate, workflow }] } };
      if (url.pathname.endsWith('/reject') && method === 'POST') {
        attempts += 1;
        requestBodies.push(body);
        if (attempts === 1) return { status: 409, body: { title: 'Rate workflow conflict', status: 409, detail: 'only an unpublished candidate can be rejected' } };
        workflow = { id: 'review-rejected', state: 'rejected', selected_plan_name: null, effective_start: null, effective_end: null, reviewed_at: '2026-08-13T17:00:00Z', published_at: null, activated_at: null, rate_plan_version_id: null, utility_account_id: null };
        return { status: 200, body: { home_id: homeId, candidate_id: rateCandidate.id, workflow } };
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<BillingPage mode="settings" />);
    await userEvent.click(await screen.findByRole('button', { name: new RegExp(`Open official rate update ${rateCandidate.id}`) }));

    await userEvent.click(screen.getByRole('button', { name: 'Reject candidate' }));
    expect(screen.getByRole('dialog', { name: 'Reject this rate candidate?' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Reject candidate permanently' }));
    expect(await screen.findByText(/only an unpublished candidate can be rejected/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Reject candidate' }));
    expect(screen.queryByText(/only an unpublished candidate can be rejected/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Reject candidate permanently' }));
    expect(await screen.findByText(/This candidate was rejected and cannot advance/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Reject candidate' })).not.toBeInTheDocument();
    expect(attempts).toBe(2);
    expect(requestBodies).toEqual([undefined, undefined]);
  });

  it('deletes an unpublished candidate only after explicit confirmation', async () => {
    let deletedHome = '';
    let deletedCandidate = '';
    installFetchMock((path, method) => {
      const url = new URL(path, 'http://frontend.test');
      if (url.pathname.endsWith('/rate-sources/candidates') && method === 'GET') {
        return { status: 200, body: rateCandidates };
      }
      if (url.pathname.endsWith(`/rate-sources/candidates/${rateCandidate.id}`) && method === 'DELETE') {
        deletedHome = url.searchParams.get('home_id') ?? '';
        deletedCandidate = rateCandidate.id;
        return { status: 204 };
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<BillingPage mode="settings" />);

    await userEvent.click(await screen.findByRole('button', { name: new RegExp(`Open official rate update ${rateCandidate.id}`) }));
    await userEvent.click(screen.getByRole('button', { name: 'Delete candidate' }));
    const confirmation = screen.getByRole('dialog', { name: 'Delete this disposable rate candidate?' });
    await userEvent.click(within(confirmation).getByRole('button', { name: 'Delete candidate' }));
    await waitFor(() => expect({ deletedHome, deletedCandidate }).toEqual({ deletedHome: homeId, deletedCandidate: rateCandidate.id }));
  });

  it('creates a closed manual fallback payload and clears prior-home dialog state', async () => {
    const secondHomeId = '00000000-0000-0000-0000-000000000011';
    let manualPayload: Record<string, unknown> | undefined;
    let manualHome = '';
    installFetchMock((path, method, body) => {
      const url = new URL(path, 'http://frontend.test');
      if (url.pathname.endsWith('/rate-sources/manual-candidates') && method === 'POST') {
        manualHome = url.searchParams.get('home_id') ?? '';
        manualPayload = jsonRequestBody(body);
        return { status: 201, body: { home_id: homeId, created: true, candidate_id: rateCandidate.id, revision_id: rateCandidate.source.revision_id, source_id: rateCandidate.source.id, run_id: 'manual-run', state: 'review_required', canonical_input_sha256: rateCandidate.source.artifact_sha256, network_fetch_performed: false } };
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<WorkflowSwitchHarness secondHomeId={secondHomeId} />);

    await userEvent.click(await screen.findByRole('button', { name: 'Enter rates manually' }));
    await userEvent.type(screen.getByLabelText('Official source title'), 'SCE Schedule D official tariff');
    await userEvent.type(screen.getByLabelText('Tariff identifier'), 'Schedule D 2026-08-01');
    await userEvent.type(screen.getByLabelText('Official SCE HTTPS URL (optional)'), 'https://www.sce.com/regulatory/tariff-books/rates-pricing-choices');
    await userEvent.type(screen.getByLabelText('Rate plan name'), 'MANUAL-TOU-D');
    await userEvent.type(screen.getByLabelText('Effective start date'), '2026-08-01');
    await userEvent.clear(screen.getByLabelText('USD per kWh'));
    await userEvent.type(screen.getByLabelText('USD per kWh'), '0.12345678');
    await userEvent.click(screen.getByLabelText(/I attest these reusable rate facts/));
    await userEvent.click(screen.getByRole('button', { name: 'Create review-required candidate' }));
    await waitFor(() => expect(manualPayload).toBeDefined());

    expect(manualHome).toBe(homeId);
    expect(manualPayload).toMatchObject({ administrator_attests_official_source: true, rate_plan_name: 'MANUAL-TOU-D', effective_start: '2026-08-01T07:00:00.000Z', periods: [{ season: 'all', day_type: 'all', start_minute: 0, end_minute: 1440, price_per_kwh: '0.12345678' }] });
    expect(Object.keys(manualPayload ?? {})).not.toEqual(expect.arrayContaining(['customer_name', 'account_number', 'usage_kwh', 'amount_due', 'meter_reading']));

    await userEvent.click(screen.getByRole('button', { name: 'Switch test home' }));
    expect(screen.queryByRole('dialog', { name: 'Review SCE rate update' })).not.toBeInTheDocument();
    await waitFor(() => expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.some(([input]) => new URL(typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url, 'http://frontend.test').searchParams.get('home_id') === secondHomeId)).toBe(true));
  });
});
