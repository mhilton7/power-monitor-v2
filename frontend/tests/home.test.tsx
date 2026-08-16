import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HomePage } from '../src/pages/HomePage';
import { device, home } from './fixtures';
import { installFetchMock, renderWithProviders } from './render';

describe('Home', () => {
  it('consolidates multiple sensors and preserves unavailable and offline states', async () => {
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
              state: 'offline',
              measurement: absentMeasurement,
              last_committed_at: null,
              storage_status: 'unavailable',
            },
          ],
        },
      };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [device, { ...device, id: 'device-outdoor', friendly_name: 'Outdoor AC', location: 'Outdoor unit', pzem_status: 'absent', storage_status: 'unavailable' }] } };
      return path.includes('/history') ? { status: 200, body: { points: [], energy_kwh: 0, cost: '0', completeness: 1, missing_ranges: [], resolution_seconds: 300, timezone: 'UTC', usage_source: 'authenticated PZEM-004T sensor intervals only' } } : { status: 200, body: { alerts: [] } };
    });

    renderWithProviders(<HomePage />);

    expect(await screen.findByLabelText('2.48 kilowatts')).toBeInTheDocument();
    expect(screen.getAllByRole('heading', { name: 'Sensor health' })).toHaveLength(1);
    const sensorTable = screen.getByRole('table', { name: 'Sensor health and live electrical measurements' });
    expect(within(sensorTable).getAllByRole('row')).toHaveLength(3);
    const absentSensor = screen.getByRole('rowheader', { name: /Outdoor AC/ }).closest<HTMLElement>('[role="row"]');
    expect(absentSensor).not.toBeNull();
    expect(within(absentSensor!).getByText('Offline')).toBeInTheDocument();
    expect(within(absentSensor!).getAllByText('Not available').length).toBeGreaterThanOrEqual(6);
    expect(within(absentSensor!).getByText('Absent')).toBeInTheDocument();
    expect(within(absentSensor!).getByText('Unavailable')).toBeInTheDocument();
    expect(within(absentSensor!).queryByText(/0 W|0 V|0 A/)).not.toBeInTheDocument();
    expect(screen.queryByText('Unable to load this view')).not.toBeInTheDocument();
  });

  it('distinguishes a measured zero from a missing value and labels live evidence', async () => {
    installFetchMock((path) => {
      if (path.includes('/home')) return { status: 200, body: { ...home, devices: [{ ...home.devices[0]!, measurement: { ...home.devices[0]!.measurement, active_power_w: 0, voltage_v: null } }] } };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [device] } };
      return path.includes('/history') ? { status: 200, body: { points: [], energy_kwh: 0, cost: '0', completeness: 1, missing_ranges: [], resolution_seconds: 300, timezone: 'UTC', usage_source: 'authenticated PZEM-004T sensor intervals only' } } : { status: 200, body: { alerts: [] } };
    });
    renderWithProviders(<HomePage />);
    expect(await screen.findByLabelText('0 watts')).toBeInTheDocument();
    const sensor = screen.getByRole('rowheader', { name: /Main Panel Sensor/ }).closest<HTMLElement>('[role="row"]');
    expect(sensor).not.toBeNull();
    expect(within(sensor!).getByText('0 W')).toBeInTheDocument();
    expect(within(sensor!).getByText('Not available')).toBeInTheDocument();
    expect(screen.getByText(/Live heartbeat measurement · not yet committed History/)).toBeInTheDocument();
    expect(screen.getByText('$0.17 / kWh')).toBeInTheDocument();
  });

  it('sums only a verified non-overlapping live scope and changes individual sensors to kW at 1000 W', async () => {
    const indoor = {
      ...home.devices[0]!,
      measurement: { ...home.devices[0]!.measurement, active_power_w: '600.000' },
    };
    const outdoor = {
      ...home.devices[0]!,
      id: 'device-outdoor',
      friendly_name: 'Outdoor AC',
      measurement: { ...home.devices[0]!.measurement, active_power_w: '1400.000' },
    };
    installFetchMock((path) => {
      if (path.includes('/home')) return {
        status: 200,
        body: {
          ...home,
          devices: [indoor, outdoor],
          summary_scope: {
            kind: 'verified_aggregate',
            device_id: null,
            device_ids: ['device-main', 'device-outdoor'],
            aggregate: true,
            circuit_id: 'circuit-aggregate',
          },
        },
      };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [device, { ...device, id: 'device-outdoor', friendly_name: 'Outdoor AC' }] } };
      return path.includes('/history') ? { status: 200, body: { points: [], energy_kwh: 0, cost: '0', completeness: 1, missing_ranges: [], resolution_seconds: 300, timezone: 'UTC', usage_source: 'authenticated PZEM-004T sensor intervals only' } } : { status: 200, body: { alerts: [] } };
    });

    renderWithProviders(<HomePage />);

    expect(await screen.findByLabelText('2 kilowatts')).toBeInTheDocument();
    expect(screen.getByText('Combined authenticated power from 2 verified non-overlapping live sensors.')).toBeInTheDocument();
    expect(screen.getByText('2 Live')).toBeInTheDocument();
    const indoorRow = screen.getByRole('rowheader', { name: /Main Panel Sensor/ }).closest<HTMLElement>('[role="row"]');
    const outdoorRow = screen.getByRole('rowheader', { name: /Outdoor AC/ }).closest<HTMLElement>('[role="row"]');
    expect(indoorRow).not.toBeNull();
    expect(outdoorRow).not.toBeNull();
    expect(within(indoorRow!).getByText('600 W')).toBeInTheDocument();
    expect(within(outdoorRow!).getByText('1.4 kW')).toBeInTheDocument();
  });

  it('does not double-count multiple sensors without a verified aggregate scope', async () => {
    const primarySensor = { ...home.devices[0]!, measurement: { ...home.devices[0]!.measurement, active_power_w: '10.000' } };
    const childSensor = { ...home.devices[0]!, id: 'device-child', friendly_name: 'Child Circuit', measurement: { ...home.devices[0]!.measurement, active_power_w: '20.000' } };
    installFetchMock((path) => {
      if (path.includes('/home')) return { status: 200, body: { ...home, devices: [primarySensor, childSensor] } };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [device, { ...device, id: 'device-child', friendly_name: 'Child Circuit' }] } };
      return path.includes('/history') ? { status: 200, body: { points: [], energy_kwh: 0, cost: '0', completeness: 1, missing_ranges: [], resolution_seconds: 300, timezone: 'UTC', usage_source: 'authenticated PZEM-004T sensor intervals only' } } : { status: 200, body: { alerts: [] } };
    });

    renderWithProviders(<HomePage />);

    expect(await screen.findByLabelText('10 watts')).toBeInTheDocument();
    expect(screen.queryByLabelText('30 watts')).not.toBeInTheDocument();
    expect(screen.getByText('Live heartbeat measurement · not yet committed History')).toBeInTheDocument();
  });

  it('renders only the condensed home summary and keeps charts, commands, and alerts', async () => {
    const commands: string[] = [];
    installFetchMock((path, method, body) => {
      if (path.includes('/home')) return { status: 200, body: home };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [device] } };
      if (path.includes('/history')) return { status: 200, body: { points: [], energy_kwh: 0, cost: '0', completeness: 1, missing_ranges: [], resolution_seconds: 300, timezone: 'UTC', usage_source: 'authenticated PZEM-004T sensor intervals only' } };
      if (path.endsWith('/alerts')) return { status: 200, body: { alerts: [] } };
      if (path.endsWith('/commands') && method === 'POST') {
        if (typeof body !== 'string') throw new Error('Expected a JSON request body.');
        const request = JSON.parse(body) as { command_type: string };
        commands.push(request.command_type);
        return { status: 202, body: { command: { id: 'command-sync', type: request.command_type, state: 'queued' }, confirmation_token: null } };
      }
      return { status: 404, body: {} };
    });
    const { container } = renderWithProviders(<HomePage />);

    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument();
    const summary = container.querySelector<HTMLElement>('.dashboard-summary-card');
    expect(summary).not.toBeNull();
    expect(within(summary!).getByText('Today Energy')).toBeInTheDocument();
    expect(within(summary!).getByText('Today Estimated Cost')).toBeInTheDocument();
    expect(within(summary!).getByText('This Week Cost')).toBeInTheDocument();
    expect(within(summary!).getByText('Current Rate')).toBeInTheDocument();
    expect(summary!.querySelectorAll('.dashboard-summary-metric')).toHaveLength(4);
    expect(screen.queryByRole('heading', { name: 'Voltage' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Current' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Frequency' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Power Factor' })).not.toBeInTheDocument();
    expect(screen.queryByText('Today Completeness')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Power History – Today' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Daily Energy (kWh)' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Recent Activity / Commands' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Alerts & Notifications' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Sync Now/ }));
    await waitFor(() => expect(commands).toEqual(['sync_now']));
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
    await userEvent.click(await screen.findByRole('button', { name: 'Open Main Panel Sensor sensor details' }));
    expect(screen.getByText(/GiB total · .* GiB free/)).toBeInTheDocument();
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
