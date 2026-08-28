import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { adaptiveTimeTicks, groupDailyEnergy, localCalendarDay } from '../src/lib/chart';
import { HomePage } from '../src/pages/HomePage';
import { apiResponse, billing, device, history, home, homeScopes } from './fixtures';
import { installFetchMock, renderWithProviders } from './render';

describe('Home', () => {
  it('keeps power and energy charts mounted while a new reading anchor is loading', async () => {
    const requestUrl = (input: RequestInfo | URL) => typeof input === 'string'
      ? input
      : input instanceof URL
        ? input.toString()
        : input.url;
    const fetchMock = installFetchMock((path, method) => path.includes('/history')
      ? { status: 200, body: history }
      : apiResponse(path, method));
    const { queryClient } = renderWithProviders(<HomePage />);

    expect(await screen.findByTestId('usage-chart')).toBeVisible();
    expect(await screen.findByTestId('daily-chart')).toBeVisible();
    const initialHistoryRequests = fetchMock.mock.calls.filter(([input]) => requestUrl(input).includes('/history')).length;
    const settledFetch = fetchMock.getMockImplementation();
    expect(settledFetch).toBeDefined();
    fetchMock.mockImplementation((input, init) => requestUrl(input).includes('/history')
      ? new Promise<Response>(() => undefined)
      : settledFetch!(input, init));

    act(() => {
      queryClient.setQueryData(['home', homeScopes[0]!.id], {
        ...home,
        generated_at: '2026-08-13T17:33:15Z',
      });
    });

    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => requestUrl(input).includes('/history')).length).toBeGreaterThan(initialHistoryRequests));
    expect(screen.getByTestId('usage-chart')).toBeVisible();
    expect(screen.getByTestId('daily-chart')).toBeVisible();
    expect(screen.queryByText('Loading saved readings')).not.toBeInTheDocument();
    expect(screen.queryByText('Loading daily energy')).not.toBeInTheDocument();
  });

  it('limits adaptive time ticks to the available label width', () => {
    const start = Date.parse('2026-08-20T00:00:00Z');
    const end = Date.parse('2026-08-21T00:00:00Z');
    expect(adaptiveTimeTicks(start, end, 390)).toHaveLength(4);
    expect(adaptiveTimeTicks(start, end, 1440).length).toBeLessThanOrEqual(8);
    expect(adaptiveTimeTicks(start, end, 390)).toEqual(expect.arrayContaining([start, end]));
  });

  it('groups selected energy by Pacific calendar day and assigns only matched fully-contained gap evidence', () => {
    const rangeStart = Date.parse('2026-08-21T07:00:00Z');
    const rangeEnd = Date.parse('2026-08-22T07:00:00Z');
    const result = groupDailyEnergy({
      rangeStart,
      rangeEnd,
      timezone: 'America/Los_Angeles',
      points: [
        { timestamp: '2026-08-21T08:00:00Z', epoch: Date.parse('2026-08-21T08:00:00Z'), value: 1, cost: '0.2', quality: 1 },
        { timestamp: '2026-08-21T12:00:00Z', epoch: Date.parse('2026-08-21T12:00:00Z'), value: 0, cost: '0', quality: 1 },
        { timestamp: '2026-08-21T16:00:00Z', epoch: Date.parse('2026-08-21T16:00:00Z'), value: null, cost: null, quality: 0 },
        { timestamp: '2026-08-22T02:00:00Z', epoch: Date.parse('2026-08-22T02:00:00Z'), value: 2, cost: '0.4', quality: 1 },
      ],
      gaps: [
        { event_id: 'gap-recovered', start_utc: '2026-08-21T17:00:00Z', end_utc: '2026-08-21T18:00:00Z', recovered_energy_kwh: '0.4', status: 'recovered' },
        { event_id: 'gap-estimated', start_utc: '2026-08-21T19:00:00Z', end_utc: '2026-08-21T19:30:00Z', recovered_energy_kwh: null, status: 'unresolved' },
        { event_id: 'gap-cross-day', start_utc: '2026-08-22T06:30:00Z', end_utc: '2026-08-22T07:30:00Z', recovered_energy_kwh: null, status: 'unresolved' },
      ],
      estimates: [
        { event_id: 'gap-estimated', status: 'estimated', energy_kwh: '0.2' },
        { event_id: 'gap-cross-day', status: 'estimated', energy_kwh: '0.3' },
      ],
    });

    expect(localCalendarDay(Date.parse('2026-08-22T02:00:00Z'), 'America/Los_Angeles')).toBe('2026-08-21');
    expect(result.days).toHaveLength(1);
    expect(result.days[0]).toMatchObject({ acceptedEnergyKwh: 3, recoveredEnergyKwh: 0.4, estimatedEnergyKwh: 0.2, value: 3.6, hasMissingIntervals: true });
    expect(result.assignedTotalKwh).toBeCloseTo(3.6);
    expect(result.unallocated).toEqual([{ eventId: 'gap-cross-day', energyKwh: 0.3, kind: 'estimated', reason: 'partially_selected' }]);
  });

  it('keeps a missing local-day energy bucket missing rather than converting it to zero', () => {
    const result = groupDailyEnergy({
      rangeStart: Date.parse('2026-08-21T07:00:00Z'), rangeEnd: Date.parse('2026-08-22T07:00:00Z'), timezone: 'America/Los_Angeles',
      points: [{ timestamp: '2026-08-21T12:00:00Z', epoch: Date.parse('2026-08-21T12:00:00Z'), value: null, cost: null, quality: 0 }],
      gaps: [], estimates: [],
    });
    expect(result.days[0]?.value).toBeNull();
    expect(result.assignedTotalKwh).toBeNull();
  });
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
              storage_status: null,
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
              storage_status: null,
            },
          ],
        },
      };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [{ ...device, storage_status: null, acknowledgement: null }, { ...device, id: 'device-outdoor', friendly_name: 'Outdoor AC', location: 'Outdoor unit', pzem_status: 'absent', storage_status: null, acknowledgement: null }] } };
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
    expect(within(absentSensor!).getAllByText('Not available').length).toBeGreaterThanOrEqual(5);
    expect(within(absentSensor!).getByText('Absent')).toBeInTheDocument();
    expect(within(absentSensor!).getByText('Received')).toBeInTheDocument();
    expect(within(sensorTable).queryByText('microSD')).not.toBeInTheDocument();
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
    expect(screen.getByText(/History may take a moment to show the newest accepted reading/)).toBeInTheDocument();
  });

  it('sums only a verified non-overlapping live scope and changes individual sensors to kW at 1000 W', async () => {
    const historyRequests: string[] = [];
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
          summary_scope: { kind: 'selected_sensor', device_id: 'device-main', aggregate: false },
          aggregate_measurement: null,
        },
      };
      if (path.includes('/circuits?')) return { status: 200, body: { circuits: [{ id: 'circuit-aggregate', home_id: device.home_id, name: 'Main service', description: null, purpose: 'whole_home_total', is_home_total: true, is_billing_source: true, aggregate_mode: 'verified_sum', non_overlapping_confirmed: true, device_ids: ['device-main', 'device-outdoor'] }] } };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [device, { ...device, id: 'device-outdoor', friendly_name: 'Outdoor AC' }] } };
      if (path.includes('/history')) {
        historyRequests.push(path);
        return { status: 200, body: { points: [], energy_kwh: 0, cost: '0', completeness: 1, missing_ranges: [], resolution_seconds: 300, timezone: 'UTC', usage_source: 'authenticated PZEM-004T sensor intervals only' } };
      }
      return { status: 200, body: { alerts: [] } };
    });

    renderWithProviders(<HomePage />);

    expect(await screen.findByLabelText('2 kilowatts')).toBeInTheDocument();
    expect(screen.getByText('Main service combines live power from 2 non-overlapping sensors.')).toBeInTheDocument();
    expect(screen.getByText('2 Live')).toBeInTheDocument();
    const liveCard = screen.getByRole('heading', { name: 'Live Power Usage' }).closest('.card');
    expect(liveCard).not.toBeNull();
    expect(within(liveCard as HTMLElement).queryByText('Voltage')).not.toBeInTheDocument();
    expect(within(liveCard as HTMLElement).queryByText('Current')).not.toBeInTheDocument();
    expect(within(liveCard as HTMLElement).queryByText('Frequency')).not.toBeInTheDocument();
    expect(within(liveCard as HTMLElement).queryByText('PF')).not.toBeInTheDocument();
    const indoorRow = screen.getByRole('rowheader', { name: /Main Panel Sensor/ }).closest<HTMLElement>('[role="row"]');
    const outdoorRow = screen.getByRole('rowheader', { name: /Outdoor AC/ }).closest<HTMLElement>('[role="row"]');
    expect(indoorRow).not.toBeNull();
    expect(outdoorRow).not.toBeNull();
    expect(within(indoorRow!).getByText('600 W')).toBeInTheDocument();
    expect(within(outdoorRow!).getByText('1.4 kW')).toBeInTheDocument();
    await waitFor(() => expect(historyRequests.length).toBeGreaterThan(0));
    expect(historyRequests.every((path) => path.includes('aggregate_circuit_id=circuit-aggregate') && !path.includes('device_id='))).toBe(true);
  });

  it('shows a clearly labeled partial Main service total when one member reading is missing', async () => {
    const indoor = { ...home.devices[0]!, measurement: { ...home.devices[0]!.measurement, active_power_w: '600.000' } };
    const outdoor = { ...home.devices[0]!, id: 'device-outdoor', friendly_name: 'Outdoor AC', state: 'needs_attention' as const, measurement: { ...home.devices[0]!.measurement, active_power_w: null, pzem_status: 'timeout' } };
    installFetchMock((path) => {
      if (path.includes('/home')) return { status: 200, body: { ...home, devices: [indoor, outdoor], summary_scope: { kind: 'verified_aggregate', device_id: null, device_ids: ['device-main', 'device-outdoor'], aggregate: true, circuit_id: 'circuit-aggregate' }, aggregate_measurement: { state: 'unavailable', active_power_w: null, member_device_ids: ['device-main', 'device-outdoor'], voltage_v: null, frequency_hz: null, power_factor: null } } };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [device, { ...device, id: 'device-outdoor', friendly_name: 'Outdoor AC', pzem_status: 'timeout' }] } };
      return path.includes('/history') ? { status: 200, body: { points: [], energy_kwh: null, cost: null, completeness: 0, missing_ranges: [], resolution_seconds: 300, timezone: 'UTC', usage_source: 'authenticated PZEM-004T sensor intervals only' } } : { status: 200, body: { alerts: [] } };
    });

    renderWithProviders(<HomePage />);

    expect(await screen.findByLabelText('600 watts')).toBeInTheDocument();
    expect(screen.queryByLabelText('Power gauge unavailable')).not.toBeInTheDocument();
    expect(screen.getByText('Partial total: 1 of 2 Main service sensors are reporting. Missing sensors are not treated as zero.')).toBeInTheDocument();
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
    expect(screen.getByText(/History may take a moment to show the newest accepted reading/)).toBeInTheDocument();
  });

  it('uses the server-selected healthy sensor when the first sensor has no meter reading', async () => {
    const historyRequests: string[] = [];
    const indoor = {
      ...home.devices[0]!,
      state: 'needs_attention',
      friendly_name: 'Indoor-AC',
      measurement: { ...home.devices[0]!.measurement, active_power_w: null, pzem_status: 'timeout' },
    };
    const outdoor = {
      ...home.devices[0]!,
      id: 'device-outdoor',
      friendly_name: 'Outdoor-AC',
      measurement: { ...home.devices[0]!.measurement, active_power_w: '12.800' },
    };
    installFetchMock((path) => {
      if (path.includes('/home')) return { status: 200, body: { ...home, devices: [indoor, outdoor], summary_scope: { kind: 'selected_sensor', device_id: 'device-outdoor', device_ids: ['device-outdoor'], aggregate: false, circuit_id: null } } };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [{ ...device, friendly_name: 'Indoor-AC', pzem_status: 'timeout' }, { ...device, id: 'device-outdoor', friendly_name: 'Outdoor-AC' }] } };
      if (path.includes('/history')) { historyRequests.push(path); return { status: 200, body: { points: [], energy_kwh: 0, cost: '0', completeness: 1, missing_ranges: [], resolution_seconds: 300, timezone: 'UTC', usage_source: 'authenticated PZEM-004T sensor intervals only' } }; }
      return { status: 200, body: { alerts: [] } };
    });

    renderWithProviders(<HomePage />);

    expect(await screen.findByLabelText('12.8 watts')).toBeInTheDocument();
    expect(screen.getByText(/Outdoor-AC live reading updated/)).toBeInTheDocument();
    await waitFor(() => expect(historyRequests.every((path) => path.includes('device_id=device-outdoor'))).toBe(true));
  });

  it('renders only the condensed home summary and keeps charts, commands, and alerts', async () => {
    const commands: string[] = [];
    installFetchMock((path, method, body) => {
      if (path.includes('/home')) return { status: 200, body: home };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [device] } };
      if (path.includes('/history')) return { status: 200, body: { points: [], energy_kwh: 0, cost: '0', completeness: 1, missing_ranges: [], resolution_seconds: 300, timezone: 'UTC', usage_source: 'authenticated PZEM-004T sensor intervals only' } };
      if (path.includes('/billing')) return { status: 200, body: billing };
      if (path.includes('/bill-rate-imports')) return { status: 200, body: { extractions: [] } };
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
    expect(within(summary!).getByRole('heading', { name: 'Billing Cycle' })).toBeInTheDocument();
    expect(within(summary!).getByText('Current Usage')).toBeInTheDocument();
    expect(within(summary!).getByText('Cost to Date')).toBeInTheDocument();
    expect(within(summary!).getByText('Current Tier')).toBeInTheDocument();
    expect(within(summary!).getByText('Estimated Monthly Bill')).toBeInTheDocument();
    expect(summary!.querySelectorAll('.dashboard-summary-metric')).toHaveLength(4);
    expect(await within(summary!).findByText('Some energy is estimated because reading coverage is 0.4%.')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Voltage' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Current' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Frequency' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Power Factor' })).not.toBeInTheDocument();
    expect(screen.queryByText('Today Completeness')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Power History – Today' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Daily Energy' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Recent Activity / Commands' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Alerts & Notifications' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /^Reboot/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Reboot sensor' }));
    await waitFor(() => expect(commands).toEqual(['reboot']));
  });

  it('keeps Dashboard missing ranges as unshaded breaks rather than invented curves', async () => {
    installFetchMock((path, method) => path.includes('/history')
      ? { status: 200, body: history }
      : apiResponse(path, method));
    renderWithProviders(<HomePage />);
    const chart = await screen.findByTestId('usage-chart');
    expect(chart).toHaveAttribute('data-missing-gap-style', 'unshaded');
    expect(chart).toHaveAttribute('data-missing-range-count', '1');
  });

  it('uses authoritative yesterday and today summaries when rolling energy buckets are null', async () => {
    const energyHistoryRequests: string[] = [];
    installFetchMock((path, method) => {
      if (path.includes('/history') && path.includes('metric=energy')) {
        energyHistoryRequests.push(path);
        return { status: 200, body: { ...history, points: [{ ...history.points[0]!, value: null }], energy_kwh: null } };
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<HomePage />);

    const chart = await screen.findByTestId('daily-chart');
    expect(chart).toHaveAttribute('data-day-source', 'calendar-summaries');
    expect(chart).toHaveAttribute('data-day-count', '2');
    expect(screen.getByText('Local calendar-day total')).toBeInTheDocument();
    expect(screen.getByText('38.44 kWh')).toBeInTheDocument();
    expect(energyHistoryRequests).toHaveLength(1);
  });

  it('shows stateless delivery details and only the supported sensor commands', async () => {
    installFetchMock((path) => {
      if (path.includes('/home')) return { status: 200, body: home };
      if (path.includes('/devices?')) return { status: 200, body: { devices: [{ ...device, server_delivery_status: 'received', last_server_received_at: '2026-08-13T17:32:10Z', cumulative_energy_kwh: 42.25 }] } };
      if (path.includes('/history')) return { status: 200, body: { points: [], energy_kwh: 0, cost: '0', completeness: 1, missing_ranges: [], resolution_seconds: 300, timezone: 'UTC', usage_source: 'authenticated PZEM-004T sensor intervals only' } };
      return { status: 200, body: { alerts: [] } };
    });
    renderWithProviders(<HomePage />);
    await userEvent.click(await screen.findByRole('button', { name: 'Open Main Panel Sensor sensor details' }));
    expect(screen.getByRole('heading', { name: 'Server delivery' })).toBeInTheDocument();
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: /^Reboot/ })).toBeInTheDocument();
    expect(within(screen.getByRole('dialog')).getByRole('link', { name: /Install OTA/ })).toBeInTheDocument();
    expect(screen.queryByText(/microSD|backlog|sync now|format/i)).not.toBeInTheDocument();
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
    expect(await screen.findByRole('heading', { name: 'Daily Energy' })).toBeInTheDocument();
    expect(screen.getByText('30 Days')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Live Power Usage' })).not.toBeInTheDocument();
    expect(screen.queryByText('Estimated Monthly Bill')).not.toBeInTheDocument();
    await waitFor(() => expect(historyRequests.some((path) => path.includes('metric=energy'))).toBe(true));
  });
});
