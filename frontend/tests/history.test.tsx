import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HistoryPage } from '../src/pages/HistoryPage';
import { device, history } from './fixtures';
import { installFetchMock, renderWithProviders } from './render';

describe('History', () => {
  it('renders committed values, exact selected range, zero, and missing evidence separately', async () => {
    installFetchMock((path) => path.includes('/devices?')
      ? { status: 200, body: { devices: [device] } }
      : path.includes('/circuits?') ? { status: 200, body: { circuits: [] } } : { status: 200, body: history });
    renderWithProviders(<HistoryPage />);
    expect(await screen.findByTestId('history-chart')).toBeInTheDocument();
    expect(screen.getByText('18.74 kWh')).toBeInTheDocument();
    expect(screen.getByText(/1 missing range/)).toBeInTheDocument();
    const legend = screen.getByText(/A measured zero renders at zero/).closest('.chart-legend');
    expect(legend).not.toBeNull();
    expect(within(legend as HTMLElement).getByText(/unavailable values form a gap/)).toBeInTheDocument();
    expect(screen.getByText('Authenticated sensor evidence unavailable')).toBeInTheDocument();
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
    await screen.findByRole('option', { name: 'Verified whole home · verified aggregate' });
    await userEvent.selectOptions(screen.getByLabelText('Sensor or aggregate scope'), 'circuit:circuit-verified');
    expect(await screen.findByText(/Aggregate choices appear only after an operator verifies non-overlapping meters/)).toBeInTheDocument();
    expect(requested.some((path) => path.includes('aggregate_circuit_id=circuit-verified') && !path.includes('device_id='))).toBe(true);
  });
});
