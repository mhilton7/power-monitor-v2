import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { api } from '../src/api';
import { rateDraftSchema } from '../src/api/schemas';
import { BillingPage } from '../src/pages/BillingPage';
import { apiResponse, homeScopes } from './fixtures';
import { installFetchMock, renderWithProviders } from './render';

describe('Billing rate-source boundary', () => {
  const domesticDraft = {
    id: 'draft-domestic', home_id: homeScopes[0]!.id, artifact_sha256: 'a'.repeat(64), utility_name: 'Southern California Edison', rate_plan_name: 'DOMESTIC', rate_class: 'residential_tiered',
    plan_classification: 'seasonal_tiered', holiday_treatment: 'not_applicable', cca_or_direct_access_indicator: 'sce_generation', season_definitions: [{ name: 'summer' }], day_type_definitions: [{ name: 'all' }],
    tou_period_definitions: [{ season: 'summer', day_type: 'all', period_name: 'Tier 1', start_minute: 0, end_minute: 1440, price_per_kwh: '0.30863000' }, { season: 'summer', day_type: 'all', period_name: 'Tier 2', start_minute: 0, end_minute: 1440, price_per_kwh: '0.40962000' }],
    tier_threshold_definitions: [], reusable_price_components: [{ name: 'Base Services Charge', kind: 'daily_fixed', amount: '0.76900000', unit: 'USD/day' }],
    billing_period_start: null, billing_period_end: null, billing_period_days: null, tier_threshold_basis: 'No reusable tier threshold was established from this bill; retain the existing configured threshold and require administrator review.', candidate_complete: false,
    publication_scope: 'review_only', publishable_effective_start: null, publishable_effective_end: null,
    baseline_allocation_rule: 'Retain the existing configured reusable baseline threshold; the customer-specific bill-period allowance is not imported.', baseline_credit_rate: null, effective_start_candidate: null, effective_end_candidate: null,
    source_evidence: [{ name: 'recurring_fixed_charge', normalized_value: '0.76900000 USD/day', supporting_label: 'Base services charge' }, { name: 'per_kwh_rate', normalized_value: 'tier_1=0.30863000 USD/kWh', supporting_label: 'Tier 1 all-in rate' }, { name: 'per_kwh_rate', normalized_value: 'tier_2=0.40962000 USD/kWh', supporting_label: 'Tier 2 all-in rate' }], parser_version: 'sce-domestic-rates-v3',
    state: 'review_required', resulting_rate_version_id: null, review_required: true,
  } as const;

  it('labels PDF upload as rate-only and has no historical bill comparison surface', async () => {
    const fetchMock = installFetchMock();
    renderWithProviders(<BillingPage />);
    await userEvent.click(await screen.findByRole('button', { name: 'Import rates from SCE bill PDF' }));
    expect(screen.getByRole('heading', { name: 'Rates and reusable cost rules only' })).toBeInTheDocument();
    expect(screen.getByText(/No upload can create or change sensor readings, intervals, History/)).toBeInTheDocument();
    expect(screen.getByText(/maximum 10 MiB/)).toBeInTheDocument();
    expect(screen.queryByText(/maximum 15 MiB/)).not.toBeInTheDocument();
    expect(screen.queryByText(/actual versus/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/historical bills/i)).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input]) => {
      const path = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
      return path.includes(`/bill-rate-imports?home_id=${homeScopes[0]!.id}`);
    })).toBe(true);
  });

  it('binds a rate-source PDF upload to the exact selected home UUID', async () => {
    let uploadedHomeId: FormDataEntryValue | null = null;
    installFetchMock((path, method, body) => {
      if (path.includes('/bill-rate-imports') && method === 'POST' && body instanceof FormData) {
        uploadedHomeId = body.get('home_id');
        return { status: 422, body: { type: 'about:blank', title: 'Fixture stopped after request capture', status: 422 } };
      }
      return apiResponse(path, method);
    });
    void api.uploadRatePdf(new File(['%PDF-1.7'], 'rates.pdf', { type: 'application/pdf' }), homeScopes[0]!.id).catch(() => undefined);
    await waitFor(() => expect(uploadedHomeId).toBe(homeScopes[0]!.id));
  });

  it('fails closed if a bill parser response contains a prohibited structured field', () => {
    expect(() => rateDraftSchema.parse({
      id: 'draft-1', home_id: homeScopes[0]!.id, artifact_sha256: 'a'.repeat(64), utility_name: 'SCE', rate_plan_name: 'TOU-D', rate_class: 'residential',
      plan_classification: 'time_of_use', holiday_treatment: 'weekend_schedule',
      cca_or_direct_access_indicator: null, season_definitions: [], day_type_definitions: [], tou_period_definitions: [],
      tier_threshold_definitions: [], reusable_price_components: [], billing_period_start: null, billing_period_end: null, billing_period_days: null, tier_threshold_basis: null, candidate_complete: true, baseline_allocation_rule: null, baseline_credit_rate: null,
      publication_scope: 'complete_schedule', publishable_effective_start: null, publishable_effective_end: null,
      effective_start_candidate: null, effective_end_candidate: null, source_evidence: [], parser_version: '1.0.0',
      state: 'review_required', resulting_rate_version_id: null, review_required: true,
      total_kWh: 999,
    })).toThrow();
  });

  it('accepts a bill-rate extraction for the exact selected home', async () => {
    installFetchMock((path, method) => path.includes('/bill-rate-imports') && method === 'GET'
      ? { status: 200, body: { extractions: [domesticDraft] } }
      : apiResponse(path, method));

    await expect(api.billing(homeScopes[0]!.id)).resolves.toMatchObject({
      drafts: [{ id: domesticDraft.id, home_id: homeScopes[0]!.id }],
    });
  });

  it('fails closed if a bill-rate extraction belongs to a different home', async () => {
    installFetchMock((path, method) => path.includes('/bill-rate-imports') && method === 'GET'
      ? { status: 200, body: { extractions: [{ ...domesticDraft, home_id: '00000000-0000-0000-0000-000000000011' }] } }
      : apiResponse(path, method));

    await expect(api.billing(homeScopes[0]!.id)).rejects.toThrow('different home');
  });

  it('previews exact date-independent DOMESTIC rates and keeps an incomplete threshold review-only', async () => {
    installFetchMock((path, method) => {
      if (path.includes('/bill-rate-imports') && method === 'GET') return { status: 200, body: { extractions: [domesticDraft] } };
      return apiResponse(path, method);
    });
    renderWithProviders(<BillingPage />);
    await userEvent.click(await screen.findByRole('button', { name: /DOMESTIC/ }));
    expect(screen.getByText('seasonal tiered')).toBeInTheDocument();
    expect(screen.getByText('not applicable')).toBeInTheDocument();
    expect(screen.getAllByText('0.76900000 USD/day').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('tier_1=0.30863000 USD/kWh').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('tier_2=0.40962000 USD/kWh').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Not supplied')).toBeInTheDocument();
    expect(screen.getByText(/Billing dates are optional metadata/)).toBeInTheDocument();
    expect(screen.getAllByText(/existing configured threshold/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByRole('button', { name: 'Save corrections' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Publish version' })).toBeDisabled();
    expect(screen.queryByText(/951 kWh|354\.15/)).not.toBeInTheDocument();
  });

  it('permanently deletes a disposable PDF extraction after explicit confirmation', async () => {
    let deletedPath = '';
    installFetchMock((path, method) => {
      if (path.includes('/bill-rate-imports') && method === 'GET') {
        return { status: 200, body: { extractions: [domesticDraft] } };
      }
      if (path.endsWith(`/bill-rate-imports/${domesticDraft.id}`) && method === 'DELETE') {
        deletedPath = path;
        return { status: 204 };
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<BillingPage />);

    await userEvent.click(await screen.findByRole('button', { name: /DOMESTIC/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Delete draft' }));
    expect(screen.getByRole('dialog', { name: 'Delete this PDF rate draft?' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Delete rate draft' }));
    await waitFor(() => expect(deletedPath).toContain(`/bill-rate-imports/${domesticDraft.id}`));
  });
});
