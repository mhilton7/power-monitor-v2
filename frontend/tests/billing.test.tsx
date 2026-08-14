import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { rateDraftSchema } from '../src/api/schemas';
import { BillingPage } from '../src/pages/BillingPage';
import { installFetchMock, renderWithProviders } from './render';

describe('Billing rate-source boundary', () => {
  it('labels PDF upload as rate-only and has no historical bill comparison surface', async () => {
    installFetchMock();
    renderWithProviders(<BillingPage />);
    await userEvent.click(await screen.findByRole('button', { name: 'Import rates from SCE bill PDF' }));
    expect(screen.getByRole('heading', { name: 'Rates and reusable cost rules only' })).toBeInTheDocument();
    expect(screen.getByText(/No upload can create or change sensor readings, intervals, History/)).toBeInTheDocument();
    expect(screen.queryByText(/actual versus/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/historical bills/i)).not.toBeInTheDocument();
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
