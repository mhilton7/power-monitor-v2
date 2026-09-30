import { act, screen, waitFor, within } from '@testing-library/react';
import { LivePricing } from '../src/components/LivePricing';
import { HomePage } from '../src/pages/HomePage';
import { apiResponse, homeScopes, livePricing, session } from './fixtures';
import serializedPricing from './fixtures/live-pricing-api.json';
import { installFetchMock, renderWithProviders } from './render';

describe('live pricing on the existing dashboard', () => {
  it('renders the exact sanitized response asserted by the real backend API test', async () => {
    installFetchMock(() => ({ status: 200, body: serializedPricing }));
    renderWithProviders(<LivePricing homeId={serializedPricing.home_id} />);
    expect(await screen.findByText('$0.30/kWh')).toBeVisible();
    expect(screen.getByText('$0.60/hour')).toBeVisible();
    expect(screen.getByText('Tier 2', { exact: true })).toBeVisible();
    expect(screen.getByText(/Cycle usage: 10.5 kWh.*No higher usage tier/)).toBeVisible();
    expect(screen.queryByText(/100% of tier/)).not.toBeInTheDocument();
  });
  it('displays the server current price instead of only a billing estimate', async () => {
    installFetchMock();
    renderWithProviders(<HomePage />);
    const pricing = await screen.findByRole('region', { name: 'Current electricity pricing' });
    expect(await within(pricing).findByText('$0.172/kWh')).toBeVisible();
    expect(within(pricing).getByText('Off-Peak')).toBeVisible();
  });

  it('shows both hybrid tier and TOU context with exact server money and usage-based progress', async () => {
    installFetchMock(() => ({ status: 200, body: { ...livePricing, current_rate: { ...livePricing.current_rate,
      pricing_model: 'hybrid', tou_period: 'Peak', current_tier: 2, price_per_kwh: '0.30',
      cycle_usage_kwh: '10.5', remaining_tier_kwh: null, tier_progress_percent: '100', estimated_cost_per_hour: '0.60',
    } } }));
    renderWithProviders(<LivePricing homeId={homeScopes[0]!.id} />);
    expect(await screen.findByText('$0.30/kWh')).toBeVisible();
    expect(screen.getByText('Peak · Tier 2')).toBeVisible();
    expect(screen.getByText('$0.60/hour')).toBeVisible();
    expect(screen.getByText(/10.5 kWh.*100% of tier threshold/)).toBeVisible();
    expect(screen.getByText(/Next scheduled change: Aug 13, 2026, 05:00 PM PDT/)).toBeVisible();
  });

  it.each(['stale', 'unavailable'])('preserves the known price when load is %s, without inventing a live cost', async (loadState) => {
    installFetchMock(() => ({ status: 200, body: { ...livePricing, current_rate: { ...livePricing.current_rate, load_state: loadState, estimated_cost_per_hour: null } } }));
    renderWithProviders(<LivePricing homeId={homeScopes[0]!.id} />);
    expect(await screen.findByText('$0.172/kWh')).toBeVisible();
    expect(screen.queryByText(/\$.*\/hour/)).not.toBeInTheDocument();
    expect(screen.getByText('Not available')).toBeVisible();
  });

  it('distinguishes a legitimate zero price and zero load from incomplete usage', async () => {
    const fetch = installFetchMock(() => ({ status: 200, body: { ...livePricing, current_rate: { ...livePricing.current_rate, price_per_kwh: '0', estimated_cost_per_hour: '0' } } }));
    const { queryClient } = renderWithProviders(<LivePricing homeId={homeScopes[0]!.id} />);
    expect(await screen.findByText('$0.00/kWh')).toBeVisible();
    expect(screen.getByText('$0.00/hour')).toBeVisible();
    fetch.mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ ...livePricing, current_rate_state: 'incomplete_usage', current_rate: { ...livePricing.current_rate, pricing_state: 'incomplete_usage', price_per_kwh: null, estimated_cost_per_hour: null } }), { status: 200, headers: { 'Content-Type': 'application/json' } })));
    await act(() => queryClient.invalidateQueries({ queryKey: ['live-pricing'] }));
    expect(await screen.findByText(/Account usage is incomplete/)).toBeVisible();
    expect(screen.queryByText('$0.00/hour')).not.toBeInTheDocument();
  });

  it.each(['rates.view', 'billing.view'])('does not request or expose prices without %s', (permission) => {
    const fetch = installFetchMock();
    renderWithProviders(<LivePricing homeId={homeScopes[0]!.id} />, { currentSession: { ...session, user: { ...session.user, permissions: session.user.permissions.filter((value) => value !== permission) } } });
    expect(screen.getByText(/Permission required/)).toBeVisible();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('makes failed fetches visible and never represents them as zero prices', async () => {
    installFetchMock(() => ({ status: 503, body: { title: 'Unavailable' } }));
    renderWithProviders(<LivePricing homeId={homeScopes[0]!.id} />);
    expect(await screen.findByText(/Current pricing could not be refreshed/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Retry pricing' })).toBeVisible();
    expect(screen.queryByText('$0.00/kWh')).not.toBeInTheDocument();
  });

  it.each([null, '2026-08-14T00:00:00Z'])('distinguishes an absent assignment from a scheduled one (%s)', async (nextRefresh) => {
    installFetchMock(() => ({ status: 200, body: { ...livePricing, current_rate: null, current_rate_state: 'unconfigured', next_pricing_refresh_at: nextRefresh } }));
    renderWithProviders(<LivePricing homeId={homeScopes[0]!.id} />);
    expect(await screen.findByText(nextRefresh ? 'Scheduled rate pending activation.' : 'No active electricity rate assignment.')).toBeVisible();
    expect(screen.queryByText('$0.00/kWh')).not.toBeInTheDocument();
  });

  it('rejects a response from another home', async () => {
    installFetchMock(() => ({ status: 200, body: { ...livePricing, home_id: 'other-home' } }));
    renderWithProviders(<LivePricing homeId={homeScopes[0]!.id} />);
    expect(await screen.findByText(/Current pricing could not be refreshed/)).toBeVisible();
    expect(screen.queryByText('$0.172/kWh')).not.toBeInTheDocument();
  });

  it('refreshes a clock-only boundary using server time without invalidating History', async () => {
    let calls = 0;
    const fetch = installFetchMock((path, method) => {
      if (!path.includes('/home/pricing')) return apiResponse(path, method);
      calls += 1;
      return { status: 200, body: { ...livePricing, generated_at: '2026-08-13T23:59:59.500Z',
        current_rate: { ...livePricing.current_rate, load_fresh_until: null, price_per_kwh: calls === 1 ? '0.172' : '0.30' },
      } };
    });
    renderWithProviders(<LivePricing homeId={homeScopes[0]!.id} />);
    expect(await screen.findByText('$0.172/kWh')).toBeVisible();
    await waitFor(() => expect(screen.getByText('$0.30/kWh')).toBeVisible(), { timeout: 1500 });
    expect(calls).toBe(2);
    expect(fetch.mock.calls.every(([path]) => (typeof path === 'string' ? path : path instanceof URL ? path.href : path.url).includes('/home/pricing'))).toBe(true);
  });

  it('expires a fresh-load estimate even if the deadline refresh fails', async () => {
    let calls = 0;
    installFetchMock(() => ++calls === 1 ? { status: 200, body: { ...livePricing,
      generated_at: '2026-08-13T17:32:39.500Z',
    } } : { status: 503, body: { title: 'Unavailable' } });
    renderWithProviders(<LivePricing homeId={homeScopes[0]!.id} />);
    expect(await screen.findByText('$0.43/hour')).toBeVisible();
    expect(await screen.findByText(/Stale load/)).toBeVisible();
    expect(screen.queryByText('$0.43/hour')).not.toBeInTheDocument();
  });

  it('never shows expired cached pricing while a resumed request is blocked', async () => {
    const fetch = installFetchMock();
    fetch.mockImplementation(() => new Promise<Response>(() => undefined));
    const { queryClient } = renderWithProviders(<LivePricing homeId={homeScopes[0]!.id} />);
    act(() => {
      queryClient.setQueryData(['live-pricing', homeScopes[0]!.id], {
        ...livePricing, next_pricing_refresh_at: '2026-08-13T17:32:40Z',
      }, { updatedAt: Date.now() - 60_000 });
    });
    expect(await screen.findByText(/Updating the scheduled electricity price/)).toBeVisible();
    expect(screen.getByText(/Stale load/)).toBeVisible();
    expect(screen.queryByText('$0.43/hour')).not.toBeInTheDocument();
    expect(screen.queryByText('$0.172/kWh')).not.toBeInTheDocument();
  });
});
