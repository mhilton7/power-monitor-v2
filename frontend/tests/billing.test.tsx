import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { api } from '../src/api';
import { rateDraftSchema } from '../src/api/schemas';
import { BillingPage } from '../src/pages/BillingPage';
import { apiResponse, billing, homeScopes } from './fixtures';
import { installFetchMock, renderWithProviders } from './render';

describe('Billing rate-source boundary', () => {
  const domesticDraft = {
    id: 'draft-domestic', home_id: homeScopes[0]!.id, artifact_sha256: 'a'.repeat(64), utility_name: 'Southern California Edison', rate_plan_name: 'DOMESTIC', rate_class: 'residential_tiered',
    plan_classification: 'seasonal_tiered', holiday_treatment: 'not_applicable', cca_or_direct_access_indicator: 'sce_generation', season_definitions: [{ name: 'summer' }], day_type_definitions: [{ name: 'all' }],
    tou_period_definitions: [{ season: 'summer', day_type: 'all', period_name: 'Tier 1', start_minute: 0, end_minute: 1440, price_per_kwh: '0.30863000', tier_end_kwh: '579.0' }, { season: 'summer', day_type: 'all', period_name: 'Tier 2', start_minute: 0, end_minute: 1440, price_per_kwh: '0.40962000', tier_start_kwh: '579.0' }],
    tier_threshold_definitions: [{ start_kwh: '0', end_kwh: '579.0' }, { start_kwh: '579.0', end_kwh: null }], tier_threshold_rule: { rule_type: 'daily_allowance', season: 'summer', kwh_per_day: '19.3', source_allowance_kwh: '579.0', source_billing_days: 30, tier1_boundary_inclusive: true }, reusable_price_components: [{ name: 'Base Services Charge', kind: 'daily_fixed', amount: '0.76900000', unit: 'USD/day' }],
    billing_period_start: null, billing_period_end: null, billing_period_days: 30, tier_threshold_basis: 'bill_baseline_allowance', candidate_complete: true,
    publication_scope: 'complete_schedule', publishable_effective_start: null, publishable_effective_end: null,
    baseline_allocation_rule: 'daily_allowance', baseline_credit_rate: null, effective_start_candidate: null, effective_end_candidate: null,
    source_evidence: [{ name: 'recurring_fixed_charge', normalized_value: '0.76900000 USD/day', supporting_label: 'Base services charge' }, { name: 'per_kwh_rate', normalized_value: 'tier_1=0.30863000 USD/kWh', supporting_label: 'Tier 1 all-in rate' }, { name: 'per_kwh_rate', normalized_value: 'tier_2=0.40962000 USD/kWh', supporting_label: 'Tier 2 all-in rate' }], parser_version: 'sce-domestic-rates-v4',
    state: 'review_required', resulting_rate_version_id: null, review_required: true,
  } as const;

  it('shows four plain-language sections and does not confirm a tier with low reading coverage', async () => {
    installFetchMock();
    renderWithProviders(<BillingPage />);

    expect(await screen.findByRole('heading', { name: 'Current Rate Plan' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Current Billing Cycle' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Tier Breakdown' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Cost Summary' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'SCE rate update' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Imported bill rates' })).toBeInTheDocument();
    expect(screen.getByText('SCE Domestic')).toBeInTheDocument();
    expect(screen.getAllByText('$0.30863/kWh')).toHaveLength(2);
    expect(screen.getAllByText('$0.40962/kWh')).toHaveLength(2);
    expect(screen.getByText('$0.769/day')).toBeInTheDocument();
    expect(screen.getByText('19.3 kWh per billing day')).toBeInTheDocument();
    expect(screen.getByText('Billing source: Main service')).toBeInTheDocument();
    expect(screen.getAllByText('Tier not confirmed').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/Complete reading coverage is required/)).toBeInTheDocument();
    expect(screen.getByText('Not enough data to estimate the full bill yet.')).toBeInTheDocument();
  });

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

  it('renders exact Tier 1, Tier 2, service-charge and 24-hour projection evidence', async () => {
    const cycle = {
      ...billing.accounts[0]!.current_billing_cycle,
      saved_usage_kwh: '951', reading_coverage: '1', tier_state: 'tier_2', tier_1_allowance_kwh: '579', tier_1_remaining_kwh: '0', amount_above_tier_1_kwh: '372',
      tier_1_usage_kwh: '579', tier_2_usage_kwh: '372', tier_1_cost: '178.69677', tier_2_cost: '152.37864', service_charge: '23.07', cost_to_date: '354.14541', estimated_energy_charges: '331.07541', estimated_fixed_charges: '23.07', estimated_total: '354.14541',
      tier_breakdown: { tier_1: { usage_kwh: '579', allowance_kwh: '579', remaining_kwh: '0', rate_per_kwh: '0.30863', cost: '178.69677' }, tier_2: { usage_kwh: '372', starts_above_kwh: '579', rate_per_kwh: '0.40962', cost: '152.37864' }, service_charge_to_date: '23.07', total_to_date: '354.14541' },
      projection: { status: 'available', projected_usage_kwh: '951', projected_tier_1_usage_kwh: '579', projected_tier_2_usage_kwh: '372', projected_tier_1_cost: '178.69677', projected_tier_2_cost: '152.37864', projected_service_charge: '23.07', projected_total: '354.14541', confidence: 'high', confidence_reasons: ['More than 24 hours of complete Main service readings.'] },
    };
    installFetchMock((path, method) => new URL(path, 'http://frontend.test').pathname.endsWith('/billing')
      ? { status: 200, body: { ...billing, accounts: [{ ...billing.accounts[0]!, current_billing_cycle: cycle }] } }
      : apiResponse(path, method));
    renderWithProviders(<BillingPage />);
    const tierCard = (await screen.findByRole('heading', { name: 'Tier Breakdown' })).closest('.card');
    const costCard = screen.getByRole('heading', { name: 'Cost Summary' }).closest('.card');
    expect(tierCard).not.toBeNull();
    expect(costCard).not.toBeNull();
    expect(screen.getAllByText('Tier 2').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('579.0 kWh for this billing cycle')).toBeInTheDocument();
    expect(screen.getAllByText('579 kWh').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('372 kWh').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('$354.15').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('High confidence')).toBeInTheDocument();
    expect(screen.getByText('Projected total for the current billing cycle')).toBeInTheDocument();
    expect(screen.getByRole('progressbar', { name: 'Billing-cycle tier usage' })).toHaveAttribute('aria-valuenow', '951');
  });

  it('shows numeric Tier 2 zero before the threshold instead of unavailable', async () => {
    const current = billing.accounts[0]!.current_billing_cycle;
    const cycle = { ...current, saved_usage_kwh: '420', reading_coverage: '1', tier_state: 'tier_1', tier_breakdown: { tier_1: { usage_kwh: '420', allowance_kwh: '579', remaining_kwh: '159', rate_per_kwh: '0.30863', cost: '129.6246' }, tier_2: { usage_kwh: '0', starts_above_kwh: '579', rate_per_kwh: '0.40962', cost: '0' }, service_charge_to_date: '12.304', total_to_date: '141.9286' } };
    installFetchMock((path, method) => new URL(path, 'http://frontend.test').pathname.endsWith('/billing')
      ? { status: 200, body: { ...billing, accounts: [{ ...billing.accounts[0]!, current_billing_cycle: cycle }] } }
      : apiResponse(path, method));
    renderWithProviders(<BillingPage />);
    const tierCard = (await screen.findByRole('heading', { name: 'Tier Breakdown' })).closest('.card');
    expect(tierCard).not.toBeNull();
    expect(tierCard!.textContent).toContain('Tier 2 usage');
    expect(tierCard!.textContent).toContain('0.0 kWh');
    expect(tierCard!.textContent).toContain('Tier 2 cost');
    expect(tierCard!.textContent).toContain('$0.00');
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

  it('previews a complete structured summer threshold with friendly labels', async () => {
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
    expect(screen.getByText('Residential tiered')).toBeInTheDocument();
    expect(screen.getByText('SCE generation service')).toBeInTheDocument();
    expect(screen.getByText('579 kWh')).toBeInTheDocument();
    expect(screen.getByText('19.3 kWh/day')).toBeInTheDocument();
    expect(screen.getByText('579 kWh for this 30-day bill')).toBeInTheDocument();
    expect(screen.getByText(/stored as 19.3 kWh per billing day/)).toBeInTheDocument();
    expect(screen.queryByText(/Exact rates extracted; reusable threshold still required/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save corrections' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Publish version' })).toBeEnabled();
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
