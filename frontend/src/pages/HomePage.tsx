import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, ArrowRight, CalendarDays, ChevronRight, CircleDollarSign, Clock3, Info, RefreshCw, RotateCcw, Server, UploadCloud, Waves, Zap } from 'lucide-react';
import { useMemo, useState, type ReactNode } from 'react';
import { Area, AreaChart, Bar, BarChart, Brush, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { api } from '../api';
import type { DeviceDetail, HomeData } from '../api/schemas';
import { PermissionGate } from '../auth/PermissionGate';
import { SensorDrawer } from '../components/SensorDrawer';
import { HeartbeatAge } from '../components/HeartbeatAge';
import { Card, ConfirmDialog, EmptyState, ErrorState, Loading, Notice, StatusPill } from '../components/ui';
import { useHomeScope } from '../home/useHomeScope';
import { chartAxisFormat, chartTick, dateTime, money, numeric, percent, resolveDisplayTimezone, timeAgo } from '../lib/format';
import './HomePage.css';

type SensorSummary = HomeData['devices'][number];

function historyParams(homeId: string, scope: { deviceId?: string; aggregateCircuitId?: string }, from: Date, to: Date, metric: string, resolutionSeconds?: number) {
  const query = new URLSearchParams({ home_id: homeId, from: from.toISOString(), to: to.toISOString(), metric });
  if (scope.aggregateCircuitId) query.set('aggregate_circuit_id', scope.aggregateCircuitId);
  else if (scope.deviceId) query.set('device_id', scope.deviceId);
  if (resolutionSeconds) query.set('resolution_seconds', String(resolutionSeconds));
  return query;
}

function PowerGauge({ watts, maxWatts = 10_000 }: { watts: number | null | undefined; maxWatts?: number }) {
  const ratio = watts === null || watts === undefined ? 0 : Math.min(1, Math.max(0, watts / maxWatts));
  const dash = ratio * 220;
  const gaugeLabel = watts === null || watts === undefined
    ? 'Power gauge unavailable'
    : `Power gauge ${Math.round(ratio * 100)} percent of ${numeric(maxWatts / 1000, 'kilowatts', 0)}`;

  return <div className="dashboard-power-gauge" aria-label={gaugeLabel}>
    <svg viewBox="0 0 220 140" role="img" aria-hidden="true">
      <path d="M25 120 A88 88 0 0 1 195 120" className="dashboard-gauge-track" pathLength="220" />
      <path d="M25 120 A88 88 0 0 1 195 120" className="dashboard-gauge-value" pathLength="220" strokeDasharray={`${dash} 220`} />
      <Zap x="92" y="51" width="36" height="36" />
    </svg>
    <div><span>0 kW</span><span>{maxWatts / 1000} kW</span></div>
  </div>;
}

function SummaryMetric({ icon, label, value, detail, unavailable = false }: {
  icon: ReactNode;
  label: string;
  value: string;
  detail?: string;
  unavailable?: boolean;
}) {
  return <div className={`dashboard-summary-metric${unavailable ? ' dashboard-summary-unavailable' : ''}`}>
    <div className="dashboard-summary-label">{icon}<span>{label}</span></div>
    <strong>{value}</strong>
    {detail && <small>{detail}</small>}
  </div>;
}

function humanizeHealth(value: string | null | undefined) {
  if (!value) return 'Not available';
  if (['ok', 'healthy'].includes(value)) return 'Healthy';
  return value.replaceAll('_', ' ').replace(/\b\w/g, (character) => character.toUpperCase());
}

function healthTone(value: string | null | undefined) {
  if (['ok', 'healthy'].includes(value ?? '')) return 'ok';
  if (['failed', 'unhealthy', 'invalid', 'bad_crc'].includes(value ?? '')) return 'danger';
  if (value) return 'warn';
  return 'neutral';
}

function sensorEnergy(sensor: SensorSummary, data: HomeData) {
  const scope = data.summary_scope;
  if (!scope || scope.kind !== 'selected_sensor' || scope.device_id !== sensor.id) return null;
  return data.summaries.today.energy_kwh === null ? null : Number(data.summaries.today.energy_kwh);
}

function powerDisplay(watts: number | null | undefined) {
  if (watts === null || watts === undefined) return { value: '—', unit: 'W', text: 'Not available', aria: 'Not available' };
  const useKilowatts = watts >= 1000;
  const value = useKilowatts ? watts / 1000 : watts;
  const unit = useKilowatts ? 'kW' : 'W';
  const spokenUnit = useKilowatts ? 'kilowatts' : 'watts';
  return { value: numeric(value, '', 2), unit, text: numeric(value, unit, 2), aria: numeric(value, spokenUnit, 2) };
}

function livePowerScope(data: HomeData, primary: SensorSummary, branch?: { device_ids: string[] }) {
  const scope = data.summary_scope;
  if (scope?.aggregate === true || branch) {
    const deviceIds = [...new Set(scope?.aggregate === true ? scope.device_ids ?? branch?.device_ids ?? [] : branch?.device_ids ?? [])];
    const scopedSensors = deviceIds
      .map((id) => data.devices.find((sensor) => sensor.id === id))
      .filter((sensor): sensor is SensorSummary => sensor !== undefined);
    const liveSensors = scopedSensors.filter((sensor) => sensor.state === 'live' && sensor.measurement?.active_power_w !== null && sensor.measurement?.active_power_w !== undefined);
    const measuredAt = liveSensors
      .map((sensor) => sensor.measurement?.measured_at)
      .filter((value): value is string => Boolean(value))
      .sort((left, right) => new Date(left).getTime() - new Date(right).getTime())[0] ?? null;
    const aggregate = data.aggregate_measurement;
    const requiredCount = aggregate?.required_member_count ?? deviceIds.length;
    const availableCount = aggregate?.available_member_count ?? liveSensors.length;
    const memberPower = liveSensors.reduce((sum, sensor) => sum + Number(sensor.measurement?.active_power_w ?? 0), 0);
    return {
      aggregate: true,
      watts: aggregate?.active_power_w ?? (liveSensors.length > 0 ? memberPower : null),
      liveCount: availableCount,
      scopedCount: requiredCount,
      partial: Boolean(aggregate?.partial || availableCount < requiredCount),
      measuredAt,
    };
  }
  return {
    aggregate: false,
    watts: primary.measurement?.active_power_w ?? null,
    liveCount: primary.state === 'live' && primary.measurement?.active_power_w !== null && primary.measurement?.active_power_w !== undefined ? 1 : 0,
    scopedCount: 1,
    partial: false,
    measuredAt: primary.measurement?.measured_at ?? null,
  };
}

function dailyTick(epoch: number, timezone: string) {
  return new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: timezone }).format(new Date(epoch));
}

function SensorMetric({ label, children, className = '' }: { label: string; children: ReactNode; className?: string }) {
  return <div className={`dashboard-sensor-cell ${className}`} role="cell">
    <span className="dashboard-sensor-mobile-label">{label}</span>
    <span>{children}</span>
  </div>;
}

function SensorHealthPanel({ data, details, onSelect }: {
  data: HomeData;
  details: DeviceDetail[];
  onSelect: (device: DeviceDetail) => void;
}) {
  const online = data.devices.filter((sensor) => sensor.state === 'live').length;
  const offline = data.devices.filter((sensor) => sensor.state === 'offline').length;
  const attention = data.devices.length - online - offline;
  const aggregate = [`${online} online`, `${offline} offline`, ...(attention > 0 ? [`${attention} needs attention`] : [])].join(' · ');

  return <Card className="dashboard-sensor-health">
    <header className="dashboard-section-heading">
      <div><h2>Sensor health</h2><p>{aggregate}</p></div>
      <p className="dashboard-sensor-scope"><Info aria-hidden="true" /> Each row shows one sensor. Sensors are combined only inside a service branch whose members were confirmed not to overlap.</p>
    </header>
    <div className="dashboard-sensor-table" role="table" aria-label="Sensor health and live electrical measurements">
      <div className="dashboard-sensor-row dashboard-sensor-header" role="row">
        {['Sensor', 'Status', 'Last reading', 'Power', 'Voltage', 'Current', 'Frequency', 'PF', 'Energy', 'PZEM', 'Server delivery', 'Firmware'].map((label) => <span key={label} role="columnheader">{label}</span>)}
        <span role="columnheader"><span className="sr-only">Sensor details</span></span>
      </div>
      {data.devices.map((sensor) => {
        const detail = details.find((entry) => entry.id === sensor.id);
        const measurement = sensor.measurement;
        const pzem = detail?.pzem_status ?? measurement?.pzem_status;
        const energy = detail?.cumulative_energy_kwh ?? sensor.cumulative_energy_kwh ?? sensorEnergy(sensor, data);
        const delivery = detail?.server_delivery_status ?? sensor.server_delivery_status;
        const subtitle = detail?.location ?? sensor.location ?? (sensor.measurement_scope === 'energy_only' ? 'Individual energy scope' : null);
        return <div className="dashboard-sensor-row" role="row" key={sensor.id}>
          <div className="dashboard-sensor-identity" role="rowheader">
            <span className="dashboard-sensor-icon" aria-hidden="true"><Activity /></span>
            <div><strong title={sensor.friendly_name}>{sensor.friendly_name}</strong>{subtitle && <small title={subtitle}>{subtitle}</small>}</div>
          </div>
          <SensorMetric label="Status"><StatusPill state={sensor.state} label={sensor.state === 'live' ? 'Online' : humanizeHealth(sensor.state)} /></SensorMetric>
          <SensorMetric label="Last reading"><HeartbeatAge timestamp={sensor.last_server_received_at ?? measurement?.measured_at ?? sensor.heartbeat_at} /></SensorMetric>
          <SensorMetric label="Power">{powerDisplay(measurement?.active_power_w).text}</SensorMetric>
          <SensorMetric label="Voltage">{numeric(measurement?.voltage_v, 'V', 1)}</SensorMetric>
          <SensorMetric label="Current">{numeric(measurement?.current_a, 'A', 2)}</SensorMetric>
          <SensorMetric label="Frequency">{numeric(measurement?.frequency_hz, 'Hz', 2)}</SensorMetric>
          <SensorMetric label="PF">{numeric(measurement?.power_factor, '', 2)}</SensorMetric>
          <SensorMetric label="Energy">{numeric(energy, 'kWh', 2)}</SensorMetric>
          <SensorMetric label="PZEM" className={`dashboard-health-text dashboard-health-${healthTone(pzem)}`}>{humanizeHealth(pzem)}</SensorMetric>
          <SensorMetric label="Server delivery" className={`dashboard-health-text dashboard-health-${healthTone(delivery)}`}><Server aria-hidden="true" /> {delivery ? humanizeHealth(delivery) : sensor.last_server_received_at || sensor.last_committed_at ? 'Received' : 'Not reported'}</SensorMetric>
          <SensorMetric label="Firmware">{detail?.firmware_version ?? sensor.firmware_version ?? 'Not reported'}</SensorMetric>
          <div className="dashboard-sensor-action" role="cell">
            <button type="button" onClick={() => detail && onSelect(detail)} disabled={!detail} aria-label={`Open ${sensor.friendly_name} sensor details`}><ChevronRight aria-hidden="true" /></button>
          </div>
        </div>;
      })}
    </div>
  </Card>;
}

function UsageTooltip({ active, payload, timezone, unit = 'kW' }: {
  active?: boolean;
  payload?: Array<{ payload?: { timestamp: string; value: string | number | null; cost: string | number | null; quality: string | number | null } }>;
  timezone: string;
  unit?: 'kW' | 'kWh';
}) {
  const point = payload?.[0]?.payload;
  if (!active || !point) return null;
  return <div className="chart-tooltip"><strong>{dateTime(point.timestamp, timezone)}</strong><span>{numeric(point.value === null ? null : Number(point.value), unit, 2)}</span><span>Estimated cost: {money(point.cost)}</span><span>Reading coverage: {percent(point.quality === null ? null : Number(point.quality))}</span></div>;
}

export function HomePage() {
  const [now] = useState(() => new Date());
  const [selectedDevice, setSelectedDevice] = useState<DeviceDetail>();
  const [rebootOpen, setRebootOpen] = useState(false);
  const queryClient = useQueryClient();
  const homeScope = useHomeScope();
  const { selectedHomeId } = homeScope;
  const preferences = useQuery({ queryKey: ['preferences'], queryFn: api.preferences });
  const refreshInterval = (preferences.data?.refresh_seconds ?? 30) * 1000;
  const dashboardDays = preferences.data?.dashboard_range === 'month' ? 30 : preferences.data?.dashboard_range === 'week' ? 7 : 1;
  const dashboardRangeLabel = dashboardDays === 1 ? 'Today' : dashboardDays === 7 ? '7 Days' : '30 Days';
  const visibleCards = useMemo(() => new Set(preferences.data?.dashboard_cards ?? ['live_power', 'energy', 'cost', 'completeness', 'alerts']), [preferences.data?.dashboard_cards]);
  const home = useQuery({ queryKey: ['home', selectedHomeId], queryFn: () => api.home(selectedHomeId), enabled: Boolean(selectedHomeId), refetchInterval: refreshInterval });
  const devices = useQuery({ queryKey: ['devices', selectedHomeId], queryFn: () => api.devices(selectedHomeId), enabled: Boolean(selectedHomeId), refetchInterval: refreshInterval });
  const circuits = useQuery({ queryKey: ['circuits', selectedHomeId], queryFn: () => api.circuits(selectedHomeId), enabled: Boolean(selectedHomeId), refetchInterval: refreshInterval });
  const billing = useQuery({ queryKey: ['billing', selectedHomeId], queryFn: () => api.billing(selectedHomeId), enabled: Boolean(selectedHomeId), refetchInterval: refreshInterval });
  const alerts = useQuery({ queryKey: ['alerts'], queryFn: api.alerts, refetchInterval: refreshInterval });
  const commandDeviceId = home.data?.summary_scope?.device_id ?? home.data?.devices[0]?.id ?? '';
  const serverAggregateCircuitId = home.data?.summary_scope?.aggregate ? home.data.summary_scope.circuit_id ?? '' : '';
  const homeTotalBranch = circuits.data?.circuits.find((branch) => branch.id === serverAggregateCircuitId)
    ?? circuits.data?.circuits.find((branch) => branch.is_billing_source)
    ?? circuits.data?.circuits.find((branch) => branch.is_home_total);
  const aggregateCircuitId = serverAggregateCircuitId || homeTotalBranch?.id || '';
  const liveScopeName = homeTotalBranch?.name ?? (aggregateCircuitId ? 'Main service' : home.data?.devices.find((sensor) => sensor.id === commandDeviceId)?.friendly_name ?? 'Selected sensor');
  const historyDeviceId = aggregateCircuitId ? '' : commandDeviceId;
  const historyScopeKey = aggregateCircuitId ? `aggregate:${aggregateCircuitId}` : `device:${historyDeviceId}`;
  const history24 = useQuery({
    queryKey: ['history', selectedHomeId, 'home-dashboard', dashboardDays, historyScopeKey],
    queryFn: () => api.history(historyParams(selectedHomeId, { deviceId: historyDeviceId, aggregateCircuitId }, new Date(now.getTime() - dashboardDays * 24 * 60 * 60 * 1000), now, 'power', dashboardDays === 1 ? 300 : 3600)),
    enabled: Boolean(selectedHomeId && (historyDeviceId || aggregateCircuitId)),
  });
  const daily = useQuery({
    queryKey: ['history', selectedHomeId, 'home-daily', dashboardDays, historyScopeKey],
    queryFn: () => api.history(historyParams(selectedHomeId, { deviceId: historyDeviceId, aggregateCircuitId }, new Date(now.getTime() - dashboardDays * 24 * 60 * 60 * 1000), now, 'energy', 86400)),
    enabled: Boolean(selectedHomeId && (historyDeviceId || aggregateCircuitId)),
  });
  const command = useMutation({ mutationFn: () => api.command(commandDeviceId, 'reboot'), onSuccess: () => { setRebootOpen(false); void queryClient.invalidateQueries({ queryKey: ['devices'] }); } });

  const primary = home.data?.devices.find((sensor) => sensor.id === commandDeviceId) ?? home.data?.devices[0];
  const selectedDeviceCurrent = selectedDevice
    ? devices.data?.devices.find((device) => device.id === selectedDevice.id) ?? selectedDevice
    : undefined;
  const measurement = primary?.measurement;
  const chartData = useMemo(() => {
    if (!history24.data) return [];
    const points = history24.data.points.map((point) => ({
      ...point,
      epoch: new Date(point.timestamp).getTime(),
      valueKw: point.value === null ? null : Number(point.value),
      gapBoundary: false,
    }));
    for (const gap of history24.data.missing_ranges) {
      const start = new Date(gap.start).getTime();
      const end = new Date(gap.end).getTime();
      if (Number.isFinite(start) && Number.isFinite(end) && end - start > 2) {
        points.push({ timestamp: new Date(start + 1).toISOString(), value: null, cost: null, quality: null, epoch: start + 1, valueKw: null, gapBoundary: true });
        points.push({ timestamp: new Date(end - 1).toISOString(), value: null, cost: null, quality: null, epoch: end - 1, valueKw: null, gapBoundary: true });
      }
    }
    return points.sort((left, right) => left.epoch - right.epoch);
  }, [history24.data]);
  const dailyData = useMemo(() => daily.data?.points.map((point) => ({ ...point, epoch: new Date(point.timestamp).getTime(), value: point.value === null ? null : Number(point.value) })) ?? [], [daily.data]);
  const hasCommittedPower = chartData.some((point) => point.valueKw !== null);
  const displayTimezone = resolveDisplayTimezone(preferences.data?.display_timezone, history24.data?.timezone ?? daily.data?.timezone);
  const powerAxis = useMemo(() => chartAxisFormat(chartData.map((point) => point.valueKw), 'kW', 62), [chartData]);
  const energyAxis = useMemo(() => chartAxisFormat(dailyData.map((point) => point.value), 'kWh', 66), [dailyData]);

  if (homeScope.isLoading) return <div className="page"><h1 className="sr-only">Home</h1><Loading label="Loading authorized homes" /></div>;
  if (homeScope.isError) return <div className="page"><h1 className="sr-only">Home</h1><ErrorState error={homeScope.error} retry={homeScope.refetch} /></div>;
  if (!selectedHomeId) return <div className="page"><h1 className="sr-only">Home</h1><EmptyState title={homeScope.homeScopes.length === 0 ? 'No authorized home' : 'Choose an active home'} detail={homeScope.homeScopes.length === 0 ? 'Your account has no authorized home scope. Home-specific data remains unavailable.' : 'Select a home from the Active home control before loading measurements.'} /></div>;
  if (home.isLoading) return <div className="page"><h1 className="sr-only">Home</h1><Loading label="Loading authenticated measurements" /></div>;
  if (home.isError) return <div className="page"><h1 className="sr-only">Home</h1><ErrorState error={home.error} retry={() => void home.refetch()} /></div>;
  const data = home.data;
  if (!data) return <div className="page"><h1 className="sr-only">Home</h1><ErrorState error={new Error('The Home response was empty.')} retry={() => void home.refetch()} /></div>;
  if (!primary) return <div className="page"><h1 className="sr-only">Home</h1><EmptyState title="No enrolled sensor" detail="Ask an administrator to create an enrollment token, then provision a headless sensor over USB." /></div>;
  const livePower = livePowerScope(data, primary, homeTotalBranch);
  const livePowerDisplay = powerDisplay(livePower.watts);
  const livePowerState = livePower.aggregate
    ? livePower.liveCount === livePower.scopedCount && livePower.scopedCount > 0 ? 'live' : livePower.liveCount > 0 ? 'needs_attention' : 'offline'
    : primary.state;
  const livePowerLabel = livePower.aggregate
    ? livePower.liveCount === livePower.scopedCount ? `${livePower.liveCount} Live` : `${livePower.liveCount}/${livePower.scopedCount} Live`
    : primary.state === 'live' ? 'Live' : undefined;
  const billingAccount = billing.data?.accounts[0];
  const billingCycle = billingAccount?.current_billing_cycle;
  const projection = billingCycle?.projection;
  const tierName = billingCycle?.tier_state === 'tier_1' ? 'Tier 1' : billingCycle?.tier_state === 'tier_2' ? 'Tier 2' : 'Not confirmed';
  const lastUpdated = data.generated_at ?? livePower.measuredAt ?? primary.heartbeat_at;
  const showSummary = visibleCards.has('energy') || visibleCards.has('cost');
  const showOverview = visibleCards.has('live_power') || showSummary;
  const liveSupportingText = livePower.aggregate
    ? livePower.liveCount === 0
      ? `${liveScopeName} has no live power reading yet; missing readings remain missing.`
      : !livePower.partial
        ? `${liveScopeName} combines live power from ${livePower.liveCount} non-overlapping sensors.`
        : `Partial total: ${livePower.liveCount} of ${livePower.scopedCount} ${liveScopeName} sensors are reporting. Missing sensors are not treated as zero.`
    : measurement?.active_power_w === null || measurement?.active_power_w === undefined
      ? 'Live meter power is unavailable; missing readings remain missing.'
      : primary.state === 'live'
        ? `${primary.friendly_name} live reading updated ${timeAgo(measurement.measured_at)}.`
        : `The sensor is ${primary.state.replaceAll('_', ' ')}; showing its latest authenticated reading.`;

  return <div className="home-dashboard dashboard-page">
    <header className="dashboard-page-heading">
      <div><h1>Dashboard</h1><p>Authenticated home energy visibility</p></div>
      <div className="dashboard-updated" title={lastUpdated ? dateTime(lastUpdated, displayTimezone) : 'No update timestamp available'}><RefreshCw aria-hidden="true" /><span>Last updated {timeAgo(lastUpdated)}</span></div>
    </header>
    {command.isSuccess && <Notice kind="success">Command {command.data.command.id} is queued. Awaiting authenticated device progress.</Notice>}

    {showOverview && <section className={`dashboard-overview${!visibleCards.has('live_power') ? ' dashboard-overview-summary-only' : ''}${!showSummary ? ' dashboard-overview-live-only' : ''}`} aria-label="Live power and home summary">
      {visibleCards.has('live_power') && <Card className="dashboard-live-card">
        <header><h2>Live Power Usage</h2><StatusPill state={livePowerState} {...(livePowerLabel ? { label: livePowerLabel } : {})} /></header>
        <div className="dashboard-live-body">
          <div className="dashboard-live-reading" aria-label={livePowerDisplay.aria}><strong>{livePowerDisplay.value}</strong><span>{livePowerDisplay.unit}</span></div>
          <PowerGauge watts={livePower.watts} />
        </div>
        <p><Waves aria-hidden="true" />{liveSupportingText}</p>
        <small>{livePower.partial ? 'This is a partial live total.' : 'History may take a moment to show the newest accepted reading.'}</small>
      </Card>}
      {showSummary && <Card title="Billing Cycle" eyebrow="Main service" className="dashboard-summary-card">
        {visibleCards.has('energy') && <SummaryMetric icon={<Zap aria-hidden="true" />} label="Current Usage" value={numeric(billingCycle?.saved_usage_kwh === null || billingCycle?.saved_usage_kwh === undefined ? null : Number(billingCycle.saved_usage_kwh), 'kWh')} detail="Saved Main service energy" unavailable={!billingCycle || billingCycle.saved_usage_kwh === null} />}
        {visibleCards.has('cost') && <SummaryMetric icon={<CalendarDays aria-hidden="true" />} label="Current Tier" value={tierName} detail={billingCycle?.tier_1_remaining_kwh === null || billingCycle?.tier_1_remaining_kwh === undefined ? 'Tier progress unavailable' : `${numeric(Number(billingCycle.tier_1_remaining_kwh), 'kWh')} remaining in Tier 1`} unavailable={!billingCycle || billingCycle.tier_state === 'not_confirmed'} />}
        {visibleCards.has('cost') && <SummaryMetric icon={<CircleDollarSign aria-hidden="true" />} label="Cost to Date" value={money(billingCycle?.cost_to_date ?? billingCycle?.estimated_total ?? null)} detail="Energy and service charges" unavailable={!billingCycle || (billingCycle.cost_to_date ?? billingCycle.estimated_total) === null} />}
        {visibleCards.has('cost') && <SummaryMetric icon={<Clock3 aria-hidden="true" />} label="Estimated Monthly Bill" value={projection && ['available', 'ready'].includes(projection.status) ? money(projection.projected_total ?? null) : 'Not available'} detail={projection && ['available', 'ready'].includes(projection.status) ? `${projection.confidence ?? 'Unrated'} confidence` : 'At least 24 hours of reliable readings required'} unavailable={!projection || !['available', 'ready'].includes(projection.status)} />}
        {billingCycle && (Number(billingCycle.reading_coverage ?? 1) < 1 || Number(billingCycle.unresolved_energy_kwh ?? 0) > 0) && <p className="dashboard-billing-warning">Estimate may be incomplete because some readings were not received.</p>}
      </Card>}
    </section>}

    <SensorHealthPanel data={data} details={devices.data?.devices ?? []} onSelect={setSelectedDevice} />

    <section className="dashboard-content" aria-label="Saved usage, commands, and alerts">
      {(visibleCards.has('live_power') || visibleCards.has('completeness')) && <Card title={`Power History – ${dashboardRangeLabel}`} eyebrow="Saved sensor readings" action={<span className="select-chip">kW</span>} className="dashboard-chart-card dashboard-power-history">
        {history24.isLoading ? <Loading label="Loading saved readings" /> : history24.isError ? <ErrorState error={history24.error} /> : history24.data && chartData.length > 0 && hasCommittedPower ? <div className="chart-wrap" data-testid="usage-chart" data-missing-gap-style="unshaded" data-missing-range-count={history24.data.missing_ranges.length}><ResponsiveContainer width="100%" height="100%"><AreaChart title={`Saved power over ${dashboardRangeLabel.toLowerCase()}`} desc="Saved sensor power. Missing readings render as unshaded breaks in the line." data={chartData} margin={{ top: 16, right: 12, bottom: 8, left: 8 }}><defs><linearGradient id="powerFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#65e692" stopOpacity={0.48} /><stop offset="100%" stopColor="#65e692" stopOpacity={0.02} /></linearGradient></defs><CartesianGrid stroke="#33413c" strokeDasharray="3 4" vertical={false} /><XAxis dataKey="epoch" type="number" domain={['dataMin', 'dataMax']} scale="time" minTickGap={70} interval="preserveStartEnd" tickFormatter={(value: number) => chartTick(value, dashboardDays * 24, displayTimezone)} tick={{ fill: '#9ca9a4', fontSize: 11 }} axisLine={false} tickLine={false} /><YAxis tick={{ fill: '#9ca9a4', fontSize: 11 }} tickFormatter={powerAxis.tick} axisLine={false} tickLine={false} width={powerAxis.width} /><Tooltip content={<UsageTooltip timezone={displayTimezone} />} /><Area type="monotone" dataKey="valueKw" stroke="#65e692" strokeWidth={2} fill="url(#powerFill)" connectNulls={false} isAnimationActive={false} /><Brush ariaLabel="Zoom saved power History" dataKey="epoch" height={28} travellerWidth={24} stroke="#65e692" fill="#151d1a" tickFormatter={() => ''} /></AreaChart></ResponsiveContainer></div> : <EmptyState title="No readings were received during this time." detail="Choose another time range or check the sensor connection." />}
        <div className="chart-footer"><Clock3 aria-hidden="true" /><span>{dateTime(new Date(now.getTime() - dashboardDays * 86_400_000).toISOString(), displayTimezone)} – {dateTime(now.toISOString(), displayTimezone)}</span>{history24.data && <span>{percent(history24.data.completeness === null ? null : Number(history24.data.completeness))} reading coverage · {history24.data.missing_ranges.length} gap{history24.data.missing_ranges.length === 1 ? '' : 's'}</span>}</div>
      </Card>}
      {visibleCards.has('energy') && <Card title="Daily Energy (kWh)" eyebrow="Saved service-branch totals" action={<span className="select-chip">{dashboardRangeLabel}</span>} className="dashboard-chart-card dashboard-daily-energy">
        {daily.isLoading ? <Loading label="Loading daily energy" /> : daily.isError ? <ErrorState error={daily.error} /> : daily.data && dailyData.length > 0 ? <div className="chart-wrap" data-testid="daily-chart"><ResponsiveContainer width="100%" height="100%"><BarChart title={`Saved daily energy over ${dashboardRangeLabel.toLowerCase()}`} desc="Saved daily energy from sensors confirmed not to overlap." data={dailyData} margin={{ top: 16, right: 8, bottom: 8, left: 8 }}><CartesianGrid stroke="#33413c" strokeDasharray="3 4" vertical={false} /><XAxis dataKey="epoch" type="number" domain={['dataMin', 'dataMax']} scale="time" minTickGap={45} interval="preserveStartEnd" tickFormatter={(value: number) => dailyTick(value, displayTimezone)} tick={{ fill: '#9ca9a4', fontSize: 11 }} axisLine={false} tickLine={false} /><YAxis tick={{ fill: '#9ca9a4', fontSize: 11 }} tickFormatter={energyAxis.tick} axisLine={false} tickLine={false} width={energyAxis.width} /><Tooltip content={<UsageTooltip timezone={displayTimezone} unit="kWh" />} /><Bar dataKey="value" fill="#65d98b" radius={[5, 5, 0, 0]} maxBarSize={32} isAnimationActive={false} /></BarChart></ResponsiveContainer></div> : <EmptyState title="No saved energy yet" detail="Daily totals appear after saved sensor readings reach the server." />}
        <div className="dashboard-energy-total"><span>Total</span><strong>{numeric(daily.data?.energy_kwh === null || daily.data?.energy_kwh === undefined ? null : Number(daily.data.energy_kwh), 'kWh')}</strong></div>
      </Card>}
      <aside className="dashboard-side-stack">
        <Card title="Recent Activity / Commands" className="dashboard-command-card">
          <div className="dashboard-command-grid">
            <PermissionGate permission="sensors.command.reboot"><button type="button" onClick={() => setRebootOpen(true)}><RotateCcw aria-hidden="true" /><span><strong>Reboot</strong><small>Restart sensor</small></span></button></PermissionGate>
            <PermissionGate permission="firmware.manage"><a href="/settings?section=firmware"><UploadCloud aria-hidden="true" /><span><strong>Install OTA</strong><small>Update firmware</small></span></a></PermissionGate>
          </div>
        </Card>
        {visibleCards.has('alerts') && <Card className="dashboard-alerts-card">
          <header className="dashboard-compact-heading"><h2>Alerts &amp; Notifications</h2><button type="button" onClick={() => window.dispatchEvent(new CustomEvent('pm:open-alerts'))}>View all <ArrowRight aria-hidden="true" /></button></header>
          <div className="dashboard-alert-list">
            {alerts.isLoading && <Loading label="Loading alerts" />}
            {alerts.isError && <ErrorState error={alerts.error} />}
            {alerts.data?.active_count === 0 && <div className="dashboard-alert-row"><span className="dashboard-alert-ok" aria-hidden="true">✓</span><div><strong>No active alerts</strong><small>All alert evidence resolved</small></div></div>}
            {alerts.data?.alerts.slice(0, 3).map((alert) => <div className="dashboard-alert-row" key={alert.id}><span className={`alert-dot alert-${alert.severity}`} aria-hidden="true" /><div><strong>{alert.type.replaceAll('_', ' ')}</strong><small>Evidence recorded {timeAgo(alert.opened_at)}</small></div><span className="sr-only">{alert.severity} alert</span></div>)}
          </div>
        </Card>}
      </aside>
    </section>

    <SensorDrawer device={selectedDeviceCurrent} open={Boolean(selectedDevice)} onClose={() => setSelectedDevice(undefined)} />
    <ConfirmDialog open={rebootOpen} title="Reboot sensor?" description={<p>Measurements pause briefly while the sensor restarts and reconnects.</p>} confirmLabel="Reboot sensor" busy={command.isPending} onCancel={() => setRebootOpen(false)} onConfirm={() => command.mutate()} />
  </div>;
}
