import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HomePage } from '../src/pages/HomePage';
import { device, home } from './fixtures';
import { installFetchMock, renderWithProviders } from './render';

describe('Home', () => {
  it('loads decimal-string telemetry when another sensor reports missing PZEM values', async () => {
    const absentMeasurement = {
      voltage_v: null,
      current_a: null,
      active_power_w: null,
      frequency_hz: null,
      power_factor: null,
      measured_at: null,
      pzem_status: 'absent',
    };
    installFetchMock((path) => {
      if (path.includes('/home')) return {
        status: 200,
        body: {
          ...home,
          devices: [
            {
              ...home.devices[0]!,
              measurement: {
                ...home.devices[0]!.measurement,
                voltage_v: '122.600',
                current_a: '20.200',
                active_power_w: '2480.000',
                frequency_hz: '60.010',
                power_factor: '0.970',
              },
            },
            {
              ...home.devices[0]!,
              id: 'device-outdoor',
              friendly_name: 'Outdoor AC',
              state: 'monitoring_disabled',
              measurement: absentMeasurement,
              last_committed_at: null,
            },
          ],
        },
      };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [device] } };
      return path.includes('/history') ? { status: 200, body: { points: [], energy_kwh: 0, cost: '0', completeness: 1, missing_ranges: [], resolution_seconds: 300, timezone: 'UTC', usage_source: 'authenticated PZEM-004T sensor intervals only' } } : { status: 200, body: { alerts: [] } };
    });

    renderWithProviders(<HomePage />);

    expect(await screen.findByLabelText('2.48 kilowatts')).toBeInTheDocument();
    const absentSensor = screen.getByRole('button', { name: 'Open Outdoor AC sensor details' });
    expect(within(absentSensor).getByText('Not available')).toBeInTheDocument();
    expect(screen.queryByText('Unable to load this view')).not.toBeInTheDocument();
  });

  it('distinguishes a measured zero from a missing value and labels live evidence', async () => {
    installFetchMock((path) => {
      if (path.includes('/home')) return { status: 200, body: { ...home, devices: [{ ...home.devices[0]!, measurement: { ...home.devices[0]!.measurement, active_power_w: 0, voltage_v: null } }] } };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [device] } };
      return path.includes('/history') ? { status: 200, body: { points: [], energy_kwh: 0, cost: '0', completeness: 1, missing_ranges: [], resolution_seconds: 300, timezone: 'UTC', usage_source: 'authenticated PZEM-004T sensor intervals only' } } : { status: 200, body: { alerts: [] } };
    });
    renderWithProviders(<HomePage />);
    expect(await screen.findByLabelText('0 kilowatts')).toBeInTheDocument();
    const voltage = screen.getByText('Voltage').closest('section');
    expect(voltage).not.toBeNull();
    expect(within(voltage!).getByText('Not available')).toBeInTheDocument();
    expect(screen.getByText(/Live heartbeat measurement · not yet committed History/)).toBeInTheDocument();
    expect(screen.getByText('$0.43')).toBeInTheDocument();
  });

  it('uses prepare and typed commit for formatting sensor storage', async () => {
    const calls: Array<{ command_type: string; idempotency_key: string; prepare_command_id?: string; confirmation_token?: string; typed_confirmation?: string }> = [];
    let formatPrepared = false;
    installFetchMock((path, method, body) => {
      if (path.includes('/home')) return { status: 200, body: home };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [{ ...device, ...(formatPrepared ? { last_command: { id: '00000000-0000-0000-0000-000000000001', type: 'format_storage_prepare', state: 'succeeded', progress_percent: 100, result_code: 'ok', result_evidence: { prepare_command_id: '00000000-0000-0000-0000-000000000001', acknowledged_records_lost: 42, unacknowledged_records_lost: 5, ready: true } } } : {}) }] } };
      if (path.includes('/history')) return { status: 200, body: { points: [], energy_kwh: 0, cost: '0', completeness: 1, missing_ranges: [], resolution_seconds: 300, timezone: 'UTC', usage_source: 'authenticated PZEM-004T sensor intervals only' } };
      if (path.endsWith('/alerts')) return { status: 200, body: { alerts: [] } };
      if (path.endsWith('/commands') && method === 'POST') {
        if (typeof body !== 'string') throw new Error('Expected a JSON request body.');
        const request = JSON.parse(body) as { command_type: string; idempotency_key: string; prepare_command_id?: string; confirmation_token?: string; typed_confirmation?: string };
        const type = request.command_type;
        calls.push(request);
        if (type === 'format_storage_prepare') formatPrepared = true;
        return type === 'format_storage_prepare'
          ? { status: 202, body: { command: { id: '00000000-0000-0000-0000-000000000001', type, state: 'queued' }, confirmation_token: 'bound-token' } }
          : { status: 202, body: { command: { id: '00000000-0000-0000-0000-000000000002', type, state: 'queued' }, confirmation_token: null } };
      }
      return { status: 404, body: {} };
    });
    renderWithProviders(<HomePage />);
    await userEvent.click(await screen.findByRole('button', { name: /Main Panel Sensor/ }));
    await userEvent.click(screen.getByRole('button', { name: /Format microSD history/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Queue command' }));
    expect(await screen.findByRole('heading', { name: 'Commit microSD history format?' })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/Type FORMAT STORAGE/), 'FORMAT STORAGE');
    await userEvent.click(screen.getByRole('button', { name: 'Queue command' }));
    await waitFor(() => expect(calls.map((request) => request.command_type)).toEqual(['format_storage_prepare', 'format_storage_commit']));
    expect(calls[0]?.idempotency_key).toMatch(/^[0-9a-f-]{36}$/i);
    expect(calls[1]).toMatchObject({ prepare_command_id: '00000000-0000-0000-0000-000000000001', confirmation_token: 'bound-token', typed_confirmation: 'FORMAT STORAGE' });
    expect(calls[1]?.idempotency_key).not.toBe(calls[0]?.idempotency_key);
  });

  it('applies persisted dashboard range and card visibility preferences', async () => {
    const historyRequests: string[] = [];
    installFetchMock((path) => {
      if (path.endsWith('/auth/preferences')) return { status: 200, body: { preferences: { dashboard_range: 'month', history_range: 'week', refresh_seconds: 60, power_unit: 'auto', energy_unit: 'auto', date_format: 'us', time_format: '12h', decimal_precision: 2, density: 'comfortable', dashboard_cards: ['energy'] } } };
      if (path.includes('/home')) return { status: 200, body: home };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [device] } };
      if (path.includes('/history')) { historyRequests.push(path); return { status: 200, body: { points: [], energy_kwh: 0, cost: '0', completeness: 1, missing_ranges: [], resolution_seconds: 86400, timezone: 'UTC', usage_source: 'authenticated PZEM-004T sensor intervals only' } }; }
      return { status: 200, body: { alerts: [], active_count: 0 } };
    });
    renderWithProviders(<HomePage />);
    expect(await screen.findByRole('heading', { name: 'Daily Energy (kWh)' })).toBeInTheDocument();
    expect(screen.getByText('30 Days')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Live Power Usage' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Current Rate' })).not.toBeInTheDocument();
    await waitFor(() => expect(historyRequests.some((path) => path.includes('metric=energy'))).toBe(true));
  });
});
