import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { api } from '../src/api';
import { rateDraftSchema } from '../src/api/schemas';
import { BillingPage } from '../src/pages/BillingPage';
import { apiResponse, homeScopes } from './fixtures';
import { installFetchMock, renderWithProviders } from './render';

describe('Billing rate-source boundary', () => {
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
      id: 'draft-1', artifact_sha256: 'a'.repeat(64), utility_name: 'SCE', rate_plan_name: 'TOU-D', rate_class: 'residential',
      cca_or_direct_access_indicator: null, season_definitions: [], day_type_definitions: [], tou_period_definitions: [],
      tier_threshold_definitions: [], reusable_price_components: [], baseline_allocation_rule: null, baseline_credit_rate: null,
      effective_start_candidate: null, effective_end_candidate: null, source_evidence: [], parser_version: '1.0.0',
      state: 'review_required', resulting_rate_version_id: null, review_required: true,
      total_kWh: 999,
    })).toThrow();
  });
});
