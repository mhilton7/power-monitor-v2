import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HistoryPage } from '../src/pages/HistoryPage';
import { device, history } from './fixtures';
import { installFetchMock, renderWithProviders } from './render';

describe('History', () => {
  afterEach(() => {
    sessionStorage.clear();
    window.history.replaceState(null, '', '/');
  });
  it('renders committed values, exact selected range, zero, and missing evidence separately', async () => {
    installFetchMock((path) => path.includes('/devices?')
      ? { status: 200, body: { devices: [device] } }
      : path.includes('/circuits?') ? { status: 200, body: { circuits: [] } } : { status: 200, body: history });
    renderWithProviders(<HistoryPage />);
    expect(await screen.findByTestId('history-chart')).toBeInTheDocument();
    expect(screen.getByText('18.74 kWh')).toBeInTheDocument();
    expect(screen.getAllByText('Some readings are missing.').length).toBeGreaterThanOrEqual(1);
    const legend = screen.getByText(/A measured zero renders at zero/).closest('.chart-legend');
    expect(legend).not.toBeNull();
    expect(within(legend as HTMLElement).getByText(/times without a reading form a gap/)).toBeInTheDocument();
    expect(screen.getByText('No reading was received during this time.')).toBeInTheDocument();
    expect(screen.getByText('0.42 kWh')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Connection gap details' })).toBeInTheDocument();
    expect(screen.getByText(/0.42 kWh recovered/)).toBeInTheDocument();
  });

  it('queries only an explicitly verified aggregate when that scope is selected', async () => {
    const requested: string[] = [];
    installFetchMock((path) => {
      if (path.includes('/devices?')) return { status: 200, body: { devices: [device] } };
      if (path.includes('/circuits?')) return { status: 200, body: { circuits: [{ id: 'circuit-verified', home_id: device.home_id, name: 'Verified whole home', aggregate_mode: 'verified_sum' }] } };
      if (path.includes('/history')) { requested.push(path); return { status: 200, body: { ...history, scope: { device_ids: ['device-main', 'device-secondary'], aggregate: true, circuit_id: 'circuit-verified' } } }; }
      return { status: 404, body: {} };
    });
    renderWithProviders(<HistoryPage />);
    await screen.findByRole('option', { name: 'Verified whole home' });
    await userEvent.selectOptions(screen.getByLabelText('Service branch or sensor'), 'circuit:circuit-verified');
    expect(await screen.findByText(/Sensors that measure the same electricity are never added together/)).toBeInTheDocument();
    expect(requested.some((path) => path.includes('aggregate_circuit_id=circuit-verified') && !path.includes('device_id='))).toBe(true);
  });

  it('uses the persisted default History range until the user changes it', async () => {
    installFetchMock((path) => {
      if (path.endsWith('/auth/preferences')) return { status: 200, body: { preferences: { dashboard_range: 'today', history_range: 'month', refresh_seconds: 60, power_unit: 'auto', energy_unit: 'auto', date_format: 'us', time_format: '12h', decimal_precision: 2, density: 'comfortable', dashboard_cards: ['live_power'] } } };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [device] } };
      if (path.includes('/circuits?')) return { status: 200, body: { circuits: [] } };
      return { status: 200, body: history };
    });
    renderWithProviders(<HistoryPage />);
    await waitFor(() => expect(screen.getByRole('button', { name: '30 days' })).toHaveAttribute('aria-pressed', 'true'));
    await userEvent.click(screen.getByRole('button', { name: '7 days' }));
    expect(screen.getByRole('button', { name: '7 days' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('shows the exact no-readings state without sensor storage wording', async () => {
    installFetchMock((path) => {
      if (path.includes('/devices?')) return { status: 200, body: { devices: [{ ...device, backlog: 199 }] } };
      if (path.includes('/circuits?')) return { status: 200, body: { circuits: [] } };
      if (path.includes('/history')) return { status: 200, body: { ...history, points: history.points.map((point) => ({ ...point, value: null, cost: null })), energy_kwh: null, cost: null, completeness: 0 } };
      return { status: 404, body: {} };
    });

    renderWithProviders(<HistoryPage />);

    expect(await screen.findByText('No readings were received during this time.')).toBeInTheDocument();
    expect(document.body.textContent?.toLowerCase()).not.toMatch(/backlog|waiting to sync|microsd/);
    expect(screen.queryByTestId('history-chart')).not.toBeInTheDocument();
  });

  it('renders valid saved points even when overall coverage is very low', async () => {
    installFetchMock((path) => {
      if (path.includes('/devices?')) return { status: 200, body: { devices: [{ ...device, backlog: 500 }] } };
      if (path.includes('/circuits?')) return { status: 200, body: { circuits: [] } };
      if (path.includes('/history')) return { status: 200, body: { ...history, completeness: '0.01', points: [{ ...history.points[0]!, value: 0 }, { ...history.points[1]!, value: null }, { ...history.points[2]!, value: '1.25' }] } };
      return { status: 404, body: {} };
    });
    renderWithProviders(<HistoryPage />);
    expect(await screen.findByTestId('history-chart')).toBeInTheDocument();
    expect(screen.getByText('1%')).toBeInTheDocument();
    expect(document.body.textContent?.toLowerCase()).not.toMatch(/backlog|waiting to sync|microsd/);
  });

  it('selects URL, session, designated Main service, then the first sensor in that order', async () => {
    const second = { ...device, id: 'device-second', friendly_name: 'Second sensor' };
    const main = { id: 'branch-main', home_id: device.home_id, name: 'Main service', description: null, purpose: 'whole_home_total', is_home_total: true, is_billing_source: true, aggregate_mode: 'verified_sum', non_overlapping_confirmed: true, device_ids: [device.id, second.id] };
    const other = { ...main, id: 'branch-other', name: 'Garage', purpose: 'electrical_section', is_home_total: false, is_billing_source: false };
    const requested: string[] = [];
    const handler = (path: string) => {
      if (path.includes('/devices?')) return { status: 200, body: { devices: [device, second] } };
      if (path.includes('/circuits?')) return { status: 200, body: { circuits: [other, main] } };
      if (path.includes('/history')) { requested.push(path); return { status: 200, body: history }; }
      return { status: 404, body: {} };
    };
    installFetchMock(handler);

    sessionStorage.setItem(`powermeter:history-scope:${device.home_id}`, 'circuit:branch-other');
    window.history.replaceState(null, '', '/history?scope=device:device-second');
    const first = renderWithProviders(<HistoryPage />);
    await waitFor(() => expect(requested.some((path) => path.includes('device_id=device-second'))).toBe(true));
    first.unmount();

    requested.length = 0;
    window.history.replaceState(null, '', '/history');
    const secondRender = renderWithProviders(<HistoryPage />);
    await waitFor(() => expect(requested.some((path) => path.includes('aggregate_circuit_id=branch-other'))).toBe(true));
    secondRender.unmount();

    requested.length = 0;
    sessionStorage.clear();
    const third = renderWithProviders(<HistoryPage />);
    await waitFor(() => expect(requested.some((path) => path.includes('aggregate_circuit_id=branch-main'))).toBe(true));
    third.unmount();

    requested.length = 0;
    installFetchMock((path) => path.includes('/devices?') ? { status: 200, body: { devices: [device, second] } } : path.includes('/circuits?') ? { status: 200, body: { circuits: [] } } : path.includes('/history') ? (requested.push(path), { status: 200, body: history }) : { status: 404, body: {} });
    renderWithProviders(<HistoryPage />);
    await waitFor(() => expect(requested.some((path) => path.includes('device_id=device-main'))).toBe(true));
  });

  it('advances only the Live display window each second without one-second API requests', async () => {
    let historyRequests = 0;
    installFetchMock((path) => {
      if (path.endsWith('/auth/preferences')) return { status: 200, body: { preferences: { dashboard_range: 'today', history_range: 'day', refresh_seconds: 15, power_unit: 'auto', energy_unit: 'auto', date_format: 'us', time_format: '12h', decimal_precision: 2, density: 'comfortable', dashboard_cards: ['live_power'] } } };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [device] } };
      if (path.includes('/circuits?')) return { status: 200, body: { circuits: [] } };
      if (path.includes('/history')) { historyRequests += 1; return { status: 200, body: history }; }
      return { status: 404, body: {} };
    });
    renderWithProviders(<HistoryPage />);
    await screen.findByTestId('history-chart');
    await userEvent.click(screen.getByRole('button', { name: 'Live' }));
    const status = await screen.findByTestId('live-timeline-status');
    await waitFor(() => expect(historyRequests).toBeGreaterThan(1));
    const firstEnd = Number(status.dataset.viewEnd);
    const firstRequestCount = historyRequests;
    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 1_100)); });
    expect(Number(screen.getByTestId('live-timeline-status').dataset.viewEnd)).toBeGreaterThan(firstEnd);
    expect(historyRequests).toBe(firstRequestCount);
    await userEvent.click(screen.getByRole('button', { name: '24 hours' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '24 hours' })).toHaveAttribute('aria-pressed', 'true'));
    const staticRequestCount = historyRequests;
    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 1_100)); });
    expect(historyRequests).toBe(staticRequestCount);
  });
});
