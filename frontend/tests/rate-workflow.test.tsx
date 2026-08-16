import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { api } from '../src/api';
import { BillingPage } from '../src/pages/BillingPage';
import { RateSourceWorkflow } from '../src/rates/RateSourceWorkflow';
import { rateCandidateSchema } from '../src/api/schemas';
import { apiResponse, billing, homeScopes, rateCandidate, rateCandidates } from './fixtures';
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
  it('rejects prohibited or uncontracted fields in normalized candidate rate facts', () => {
    expect(() => rateCandidateSchema.parse({
      ...rateCandidate,
      normalized_rates: { ...rateCandidate.normalized_rates, usage_kwh: '999.0' },
    })).toThrow();
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
    renderWithProviders(<BillingPage />);

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
    renderWithProviders(<BillingPage />);

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
    renderWithProviders(<BillingPage />);

    await userEvent.click(await screen.findByRole('button', { name: new RegExp(`Open official rate candidate ${rateCandidate.id}`) }));
    await userEvent.type(screen.getByLabelText('Effective start date'), '2026-08-01');
    await userEvent.click(screen.getByLabelText(/confirmed this exact effective range/));
    await userEvent.click(screen.getByLabelText(/confirmed the recorded source/));
    await userEvent.click(screen.getByRole('button', { name: 'Confirm candidate review' }));
    expect(await screen.findByText(/Review recorded/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Publish reviewed version' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Publish immutable version' }));
    expect(await screen.findByText(/Immutable version published/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Activate for selected account' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Activate exact account' }));
    expect(await screen.findByText(new RegExp(`Active for account ${billing.accounts[0]!.utility_account_id}`))).toBeInTheDocument();

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
    renderWithProviders(<BillingPage />);
    await userEvent.click(await screen.findByRole('button', { name: new RegExp(`Open official rate candidate ${rateCandidate.id}`) }));

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

    await userEvent.click(await screen.findByRole('button', { name: 'Manual fallback' }));
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
    expect(screen.queryByRole('dialog', { name: 'Review SCE rate candidate' })).not.toBeInTheDocument();
    await waitFor(() => expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.some(([input]) => new URL(typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url, 'http://frontend.test').searchParams.get('home_id') === secondHomeId)).toBe(true));
  });
});
