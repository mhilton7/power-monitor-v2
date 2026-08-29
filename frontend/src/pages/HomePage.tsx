import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, ArrowRight, CalendarDays, ChevronRight, CircleDollarSign, Clock3, Info, RefreshCw, RotateCcw, Server, UploadCloud, Waves, Zap } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';
import { Area, AreaChart, Bar, BarChart, Brush, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { api } from '../api';
import type { DeviceDetail, HomeData } from '../api/schemas';
import { PermissionGate } from '../auth/PermissionGate';
import { SensorDrawer } from '../components/SensorDrawer';
import { HeartbeatAge } from '../components/HeartbeatAge';
import { Card, ConfirmDialog, EmptyState, ErrorState, Loading, Notice, StatusPill } from '../components/ui';
import { useHomeScope } from '../home/useHomeScope';
import { chartAxisFormat, dateTime, dateTimeRange, money, numeric, percent, resolveDisplayTimezone, timeAgo } from '../lib/format';
import { adaptiveTimeTicks, groupDailyEnergy, localCalendarDay as localDayKey } from '../lib/chart';
import './HomePage.css';

type SensorSummary = HomeData['devices'][number];
type PowerBrushRange = { key: string; startMs: number; endMs: number };

function previousAnchorData<T>(
  previousData: T | undefined,
  previousKey: readonly unknown[] | undefined,
  currentKey: readonly unknown[],
): T | undefined {
  if (!previousKey || previousKey.length !== currentKey.length) return undefined;
  return previousKey.slice(0, -1).every((value, index) => value === currentKey[index])
    ? previousData
    : undefined;
}

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

function powerTickLabel(epoch: number, spanHours: number, timezone: string, crossesMidnight: boolean): string {
  const date = new Date(epoch);
  if (spanHours <= 6) return new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit', timeZone: timezone }).format(date);
  if (spanHours <= 48) return new Intl.DateTimeFormat('en-US', crossesMidnight
    ? { month: 'short', day: 'numeric', hour: 'numeric', timeZone: timezone }
    : { hour: 'numeric', timeZone: timezone }).format(date);
  return new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: timezone }).format(date);
}

function closestRangeIndices(points: Array<{ epoch: number }>, start: number, end: number): { startIndex: number; endIndex: number } {
  if (points.length === 0) return { startIndex: 0, endIndex: 0 };
  const startIndex = Math.max(0, points.findIndex((point) => point.epoch >= start));
  const reverseEnd = [...points].reverse().findIndex((point) => point.epoch <= end);
  const endIndex = reverseEnd < 0 ? points.length - 1 : points.length - 1 - reverseEnd;
  return { startIndex: Math.min(startIndex, endIndex), endIndex: Math.max(startIndex, endIndex) };
}

function useMeasuredWidth(initial = 720) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(initial);
  useEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(1, entry?.contentRect.width ?? initial)));
    observer.observe(element);
    return () => observer.disconnect();
  }, [initial]);
  return { ref, width };
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
        const delivery = detail?.server_delivery_status ?? detail?.synchronization?.server_delivery_status ?? sensor.server_delivery_status;
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
          <SensorMetric label="Server delivery" className={`dashboard-health-text dashboard-health-${healthTone(delivery)}`}><Server aria-hidden="true" /> {delivery ? humanizeHealth(delivery) : 'Not reported by the server'}</SensorMetric>
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
  payload?: Array<{ payload?: {
    timestamp: string; value: string | number | null; cost: string | number | null; quality: string | number | null;
    acceptedEnergyKwh?: number | null; recoveredEnergyKwh?: number; estimatedEnergyKwh?: number;
    hasMissingIntervals?: boolean; source?: 'bounded-intervals' | 'calendar-summaries';
  } }>;
  timezone: string;
  unit?: 'kW' | 'kWh';
}) {
  const point = payload?.[0]?.payload;
  if (!active || !point) return null;
  return <div className="chart-tooltip" role="status"><strong>{dateTime(point.timestamp, timezone)}</strong>{unit === 'kW' ? <span>Power: {numeric(point.value === null ? null : Number(point.value), unit, 2)}</span> : point.source === 'bounded-intervals' ? <><span>Assigned energy: {numeric(point.value === null ? null : Number(point.value), 'kWh', 2)}</span><span>Accepted interval energy: {numeric(point.acceptedEnergyKwh ?? null, 'kWh', 2)}</span><span>Recovered cumulative-meter energy: {numeric(point.recoveredEnergyKwh ?? 0, 'kWh', 2)}</span><span>Bounded estimated energy: {numeric(point.estimatedEnergyKwh ?? 0, 'kWh', 2)}</span>{point.hasMissingIntervals && <span>Some interval energy is missing or represented by separate gap evidence.</span>}</> : <span>Server calendar summary: {numeric(point.value === null ? null : Number(point.value), 'kWh', 2)}</span>}<span>Estimated cost: {money(point.cost)}</span><span>Reading coverage: {percent(point.quality === null ? null : Number(point.quality))}</span></div>;
}

export function HomePage() {
  const [now] = useState(() => new Date());
  const [powerBrushRange, setPowerBrushRange] = useState<PowerBrushRange | null>(null);
  const powerBrushDraggingRef = useRef(false);
  const powerBrushKeyboardInputRef = useRef(false);
  const pendingPowerBrushRangeRef = useRef<PowerBrushRange | null>(null);
  const { ref: powerChartRef, width: powerChartWidth } = useMeasuredWidth();
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
  const powerBrushKey = `${dashboardDays}:${historyScopeKey}`;
  const hasServerDailyComparisons = dashboardDays === 1 && home.data?.summaries.yesterday !== undefined;
  const historyAnchorMs = new Date(home.data?.generated_at ?? now).getTime();
  const history24Key = ['history', selectedHomeId, 'home-dashboard', dashboardDays, historyScopeKey, historyAnchorMs] as const;
  const dailyKey = ['history', selectedHomeId, 'home-daily', dashboardDays, historyScopeKey, historyAnchorMs] as const;
  const history24 = useQuery({
    queryKey: history24Key,
    queryFn: () => api.history(historyParams(selectedHomeId, { deviceId: historyDeviceId, aggregateCircuitId }, new Date(historyAnchorMs - dashboardDays * 24 * 60 * 60 * 1000), new Date(historyAnchorMs), 'power', dashboardDays === 1 ? 300 : 3600)),
    enabled: Boolean(home.data && selectedHomeId && (historyDeviceId || aggregateCircuitId)),
    placeholderData: (previousData, previousQuery) => previousAnchorData(previousData, previousQuery?.queryKey, history24Key),
  });
  const daily = useQuery({
    queryKey: dailyKey,
    queryFn: () => api.history(historyParams(selectedHomeId, { deviceId: historyDeviceId, aggregateCircuitId }, new Date(historyAnchorMs - dashboardDays * 24 * 60 * 60 * 1000), new Date(historyAnchorMs), 'energy', dashboardDays === 1 ? 300 : 3600)),
    enabled: Boolean(home.data && selectedHomeId && (historyDeviceId || aggregateCircuitId)),
    placeholderData: (previousData, previousQuery) => previousAnchorData(previousData, previousQuery?.queryKey, dailyKey),
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
  const [powerBrushSnapshot, setPowerBrushSnapshot] = useState<{ key: string; data: typeof chartData } | null>(null);
  const [powerBrushLocked, setPowerBrushLocked] = useState(false);
  useEffect(() => {
    const finishInteraction = () => {
      if (!powerBrushDraggingRef.current) return;
      powerBrushDraggingRef.current = false;
      const pendingRange = pendingPowerBrushRangeRef.current;
      pendingPowerBrushRangeRef.current = null;
      if (pendingRange) setPowerBrushRange(pendingRange);
    };
    window.addEventListener('pointerup', finishInteraction, true);
    window.addEventListener('pointercancel', finishInteraction, true);
    window.addEventListener('mouseup', finishInteraction, true);
    window.addEventListener('touchend', finishInteraction, true);
    return () => {
      window.removeEventListener('pointerup', finishInteraction, true);
      window.removeEventListener('pointercancel', finishInteraction, true);
      window.removeEventListener('mouseup', finishInteraction, true);
      window.removeEventListener('touchend', finishInteraction, true);
    };
  }, []);
  const intervalEnergyPoints = useMemo(() => daily.data?.points.map((point) => ({
    ...point,
    epoch: new Date(point.timestamp).getTime(),
    value: point.value === null ? null : Number(point.value),
  })) ?? [], [daily.data]);
  const rawDailyData = useMemo(() => {
    const homeData = home.data;
    const yesterday = homeData?.summaries.yesterday;
    if (intervalEnergyPoints.some((point) => point.value !== null)) return intervalEnergyPoints;
    if (dashboardDays === 1 && yesterday) {
      const anchor = new Date(homeData.generated_at ?? now);
      const summaryPoint = (timestamp: Date, summary: typeof yesterday) => ({
        timestamp: timestamp.toISOString(),
        epoch: timestamp.getTime(),
        value: summary.energy_kwh === null ? null : Number(summary.energy_kwh),
        cost: summary.cost,
        quality: summary.completeness === null ? null : Number(summary.completeness),
      });
      return [
        summaryPoint(new Date(anchor.getTime() - 86_400_000), yesterday),
        summaryPoint(anchor, homeData.summaries.today),
      ];
    }
    return intervalEnergyPoints;
  }, [dashboardDays, home.data, intervalEnergyPoints, now]);
  const activePowerBrushRange = powerBrushRange?.key === powerBrushKey ? powerBrushRange : null;
  const brushSnapshot = powerBrushSnapshot?.key === powerBrushKey ? powerBrushSnapshot.data : null;
  const powerChartData = brushSnapshot && powerBrushLocked ? brushSnapshot : chartData;
  const hasCommittedPower = powerChartData.some((point) => point.valueKw !== null);
  const displayTimezone = resolveDisplayTimezone(preferences.data?.display_timezone, home.data?.timezone ?? history24.data?.timezone ?? daily.data?.timezone);
  const powerAxis = useMemo(() => chartAxisFormat(powerChartData.map((point) => point.valueKw), 'kW', 62), [powerChartData]);
  const dataAnchorMs = historyAnchorMs;
  const defaultRangeStart = dataAnchorMs - dashboardDays * 86_400_000;
  const defaultRangeEnd = dataAnchorMs;
  const powerRangeStart = activePowerBrushRange?.startMs ?? defaultRangeStart;
  const powerRangeEnd = activePowerBrushRange?.endMs ?? defaultRangeEnd;
  const powerRangeIndices = activePowerBrushRange
    ? closestRangeIndices(powerChartData, powerRangeStart, powerRangeEnd)
    : { startIndex: 0, endIndex: Math.max(0, powerChartData.length - 1) };
  const powerRangeStartIndex = powerRangeIndices.startIndex;
  const powerRangeEndIndex = powerRangeIndices.endIndex;
  const powerTicks = useMemo(() => adaptiveTimeTicks(powerRangeStart, powerRangeEnd, powerChartWidth), [powerChartWidth, powerRangeEnd, powerRangeStart]);
  const selectedStartDay = localDayKey(powerRangeStart, displayTimezone);
  const selectedEndDay = localDayKey(powerRangeEnd, displayTimezone);
  const beginPowerBrushInteraction = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!(event.target instanceof Element) || !event.target.closest('.recharts-brush')) return;
    powerBrushDraggingRef.current = true;
    pendingPowerBrushRangeRef.current = null;
    setPowerBrushSnapshot({ key: powerBrushKey, data: chartData });
    setPowerBrushLocked(true);
  };
  const beginPowerBrushKeyboardInteraction = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!(event.target instanceof Element) || !event.target.closest('.recharts-brush')) return;
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) return;
    powerBrushKeyboardInputRef.current = true;
    queueMicrotask(() => { powerBrushKeyboardInputRef.current = false; });
  };
  const updatePowerBrushRange = (range: { startIndex?: number; endIndex?: number }) => {
    if (typeof range.startIndex !== 'number' || typeof range.endIndex !== 'number') return;
    const startMs = powerChartData[range.startIndex]?.epoch;
    const endMs = powerChartData[range.endIndex]?.epoch;
    if (startMs === undefined || endMs === undefined) return;
    const nextRange = { key: powerBrushKey, startMs, endMs };
    if (powerBrushDraggingRef.current) {
      pendingPowerBrushRangeRef.current = nextRange;
      return;
    }
    // Recharts can emit a full-range change when its parent refreshes even though
    // the user did not touch the brush. Only a captured pointer or keyboard
    // interaction is allowed to replace the timestamp-owned selection.
    if (!powerBrushKeyboardInputRef.current) return;
    setPowerBrushSnapshot({ key: powerBrushKey, data: chartData });
    setPowerBrushLocked(true);
    setPowerBrushRange(nextRange);
  };
  const resetPowerBrush = () => {
    powerBrushDraggingRef.current = false;
    powerBrushKeyboardInputRef.current = false;
    pendingPowerBrushRangeRef.current = null;
    setPowerBrushSnapshot(null);
    setPowerBrushLocked(false);
    setPowerBrushRange(null);
  };
  const resumeLivePower = () => {
    resetPowerBrush();
    void history24.refetch();
  };
  const intervalDailyAvailable = daily.data?.points.some((point) => point.value !== null) ?? false;
  const billingCycleForEnergy = billing.data?.accounts[0]?.current_billing_cycle;
  const intervalDailyResult = useMemo(() => groupDailyEnergy({
    points: intervalEnergyPoints,
    gaps: daily.data?.connection_gaps ?? [],
    estimates: billingCycleForEnergy?.energy_quality?.estimate_details ?? [],
    rangeStart: powerRangeStart,
    rangeEnd: powerRangeEnd,
    timezone: displayTimezone,
  }), [billingCycleForEnergy?.energy_quality?.estimate_details, daily.data?.connection_gaps, displayTimezone, intervalEnergyPoints, powerRangeEnd, powerRangeStart]);
  const dailyData = useMemo(() => {
    if (intervalDailyAvailable) return intervalDailyResult.days;
    const grouped = new Map<string, { timestamp: string; epoch: number; value: number | null; cost: number | null; quality: number | null; acceptedEnergyKwh: null; recoveredEnergyKwh: number; estimatedEnergyKwh: number; hasMissingIntervals: boolean; source: 'calendar-summaries' }>();
    for (const point of rawDailyData) {
      const day = localDayKey(point.epoch, displayTimezone);
      if (day < selectedStartDay || day > selectedEndDay) continue;
      const current = grouped.get(day);
      const value = point.value === null ? null : Number(point.value);
      const cost = point.cost === null ? null : Number(point.cost);
      const quality = point.quality === null ? null : Number(point.quality);
      if (!current) {
        grouped.set(day, { timestamp: point.timestamp, epoch: point.epoch, value, cost, quality, acceptedEnergyKwh: null, recoveredEnergyKwh: 0, estimatedEnergyKwh: 0, hasMissingIntervals: value === null, source: 'calendar-summaries' });
        continue;
      }
      current.value = value === null ? current.value : (current.value ?? 0) + value;
      current.cost = cost === null ? current.cost : (current.cost ?? 0) + cost;
      current.quality = quality === null ? current.quality : Math.min(current.quality ?? quality, quality);
      current.hasMissingIntervals ||= value === null;
    }
    return [...grouped.values()].sort((left, right) => left.epoch - right.epoch);
  }, [displayTimezone, intervalDailyAvailable, intervalDailyResult.days, rawDailyData, selectedEndDay, selectedStartDay]);
  const energyAxis = useMemo(() => chartAxisFormat(dailyData.map((point) => point.value), 'kWh', 66), [dailyData]);
  const dailyTotal = useMemo(() => intervalDailyAvailable ? intervalDailyResult.assignedTotalKwh : dailyData.length > 0 && dailyData.every((point) => point.value !== null)
    ? dailyData.reduce((total, point) => total + (point.value ?? 0), 0)
    : null, [dailyData, intervalDailyAvailable, intervalDailyResult.assignedTotalKwh]);
  const unallocatedKnownEnergy = intervalDailyResult.unallocated.reduce((total, gap) => total + (gap.energyKwh ?? 0), 0);

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
  const tierName = billingCycle?.tier_state === 'tier_1' ? 'Tier 1' : billingCycle?.tier_state === 'tier_2' ? 'Tier 2' : billingCycle?.tier_state === 'estimated_tier_1' ? 'Estimated Tier 1' : billingCycle?.tier_state === 'estimated_tier_2' ? 'Estimated Tier 2' : 'Not confirmed';
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
        {visibleCards.has('energy') && <SummaryMetric icon={<Zap aria-hidden="true" />} label="Current Usage" value={numeric((billingCycle?.current_usage_kwh ?? billingCycle?.saved_usage_kwh) === null || (billingCycle?.current_usage_kwh ?? billingCycle?.saved_usage_kwh) === undefined ? null : Number(billingCycle?.current_usage_kwh ?? billingCycle?.saved_usage_kwh), 'kWh')} detail="Measured, recovered, and identified estimate energy" unavailable={!billingCycle || (billingCycle.current_usage_kwh ?? billingCycle.saved_usage_kwh) === null} />}
        {visibleCards.has('cost') && <SummaryMetric icon={<CalendarDays aria-hidden="true" />} label="Current Tier" value={tierName} detail={billingCycle?.tier_1_remaining_kwh === null || billingCycle?.tier_1_remaining_kwh === undefined ? 'Tier progress unavailable' : `${numeric(Number(billingCycle.tier_1_remaining_kwh), 'kWh')} remaining in Tier 1`} unavailable={!billingCycle || billingCycle.tier_state === 'not_confirmed'} />}
        {visibleCards.has('cost') && <SummaryMetric icon={<CircleDollarSign aria-hidden="true" />} label="Cost to Date" value={money(billingCycle?.cost_to_date ?? billingCycle?.estimated_total ?? null)} detail="Energy and service charges" unavailable={!billingCycle || (billingCycle.cost_to_date ?? billingCycle.estimated_total) === null} />}
        {visibleCards.has('cost') && <SummaryMetric icon={<Clock3 aria-hidden="true" />} label="Estimated Monthly Bill" value={projection && ['available', 'ready'].includes(projection.status) ? money(projection.projected_total ?? null) : 'Not available'} detail={projection && ['available', 'ready'].includes(projection.status) ? `${projection.confidence ?? 'Unrated'} confidence` : 'At least 24 hours of reliable readings required'} unavailable={!projection || !['available', 'ready'].includes(projection.status)} />}
        {billingCycle && (billingCycle.availability_reasons.length > 0 || Number(billingCycle.reading_coverage ?? 1) < 1 || Number(billingCycle.unknown_energy_kwh ?? billingCycle.unresolved_energy_kwh ?? 0) > 0) && <p className="dashboard-billing-warning" data-reason-codes={billingCycle.availability_reasons.map((reason) => reason.code).join(' ')}>{billingCycle.availability_reasons.length > 0 ? billingCycle.availability_reasons.map((reason) => reason.message).join(' ') : billingCycle.calculation_state === 'exact' ? 'The precise power shape contains gaps, but cumulative meter energy recovered the billing total.' : `Some energy is estimated because reading coverage is ${percent(billingCycle.reading_coverage === null ? null : Number(billingCycle.reading_coverage))}.`}</p>}
      </Card>}
    </section>}

    <SensorHealthPanel data={data} details={devices.data?.devices ?? []} onSelect={setSelectedDevice} />

    <section className="dashboard-content" aria-label="Saved usage, commands, and alerts">
      {(visibleCards.has('live_power') || visibleCards.has('completeness')) && <Card title={`Power History – ${dashboardRangeLabel}`} eyebrow="Saved sensor readings" action={<div className="chart-actions"><span className="select-chip">kW</span>{activePowerBrushRange && <><button type="button" className="text-button" onClick={resetPowerBrush}>Reset zoom</button><button type="button" className="text-button" onClick={resumeLivePower}>Resume live</button></>}</div>} className="dashboard-chart-card dashboard-power-history">
        <p id="power-history-summary" className="sr-only">Saved sensor power in {displayTimezone}. Missing readings are unshaded breaks. Use the range selector to zoom; Reset zoom or Resume live returns to the full range.</p>
        {history24.isLoading ? <Loading label="Loading saved readings" /> : history24.isError ? <ErrorState error={history24.error} /> : history24.data && powerChartData.length > 0 && hasCommittedPower ? <div ref={powerChartRef} className="chart-wrap" role="group" aria-label="Saved power over time" aria-describedby="power-history-summary" data-testid="usage-chart" data-missing-gap-style="unshaded" data-missing-range-count={history24.data.missing_ranges.length} data-user-selected-range={activePowerBrushRange ? 'true' : 'false'} onPointerDownCapture={beginPowerBrushInteraction} onKeyDownCapture={beginPowerBrushKeyboardInteraction}><ResponsiveContainer width="100%" height="100%"><AreaChart accessibilityLayer data={powerChartData} margin={{ top: 16, right: 12, bottom: 8, left: 8 }}><defs><linearGradient id="powerFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#65e692" stopOpacity={0.48} /><stop offset="100%" stopColor="#65e692" stopOpacity={0.02} /></linearGradient></defs><CartesianGrid stroke="#33413c" strokeDasharray="3 4" vertical={false} /><XAxis dataKey="epoch" type="number" domain={['dataMin', 'dataMax']} scale="time" ticks={powerTicks} minTickGap={12} interval="preserveStartEnd" tickFormatter={(value: number) => powerTickLabel(value, Math.max(1, (powerRangeEnd - powerRangeStart) / 3_600_000), displayTimezone, selectedStartDay !== selectedEndDay)} tick={{ fill: '#9ca9a4', fontSize: 11 }} axisLine={false} tickLine={false} /><YAxis tick={{ fill: '#9ca9a4', fontSize: 11 }} tickFormatter={powerAxis.tick} axisLine={false} tickLine={false} width={powerAxis.width} /><Tooltip content={<UsageTooltip timezone={displayTimezone} />} wrapperStyle={{ outline: 'none' }} /><Area name="Power" type="monotone" dataKey="valueKw" stroke="#65e692" strokeWidth={2} fill="url(#powerFill)" connectNulls={false} isAnimationActive={false} /><Brush ariaLabel="Zoom saved power History" dataKey="epoch" height={28} travellerWidth={24} stroke="#65e692" fill="#151d1a" tickFormatter={() => ''} startIndex={powerRangeStartIndex} endIndex={powerRangeEndIndex} onChange={updatePowerBrushRange} /></AreaChart></ResponsiveContainer></div> : <EmptyState title="No readings were received during this time." detail="Choose another time range or check the sensor connection." />}
        <div className="chart-footer"><Clock3 aria-hidden="true" /><span className="chart-footer-range" data-testid="power-selected-range">{dateTimeRange(new Date(powerRangeStart).toISOString(), new Date(powerRangeEnd).toISOString(), displayTimezone)}</span>{history24.data && <span className="chart-footer-coverage">{percent(history24.data.completeness === null ? null : Number(history24.data.completeness))} reading coverage · {history24.data.missing_ranges.length} gap{history24.data.missing_ranges.length === 1 ? '' : 's'}</span>}</div>
      </Card>}
      {visibleCards.has('energy') && <Card title="Daily Energy" eyebrow="Energy by local calendar day" action={<div className="chart-actions"><span className="select-chip">{dashboardRangeLabel}</span><span className="select-chip">kWh</span></div>} className="dashboard-chart-card dashboard-daily-energy">
        {selectedStartDay !== selectedEndDay && dashboardDays === 1 && <p className="chart-helper">The selected 24-hour range spans two calendar days.</p>}
        {intervalDailyAvailable && <p className="chart-helper">Each bar separates accepted interval energy, recovered cumulative-meter energy, and matched bounded estimates in its tooltip.</p>}
        {daily.isLoading && !hasServerDailyComparisons ? <Loading label="Loading daily energy" /> : daily.isError && !hasServerDailyComparisons ? <ErrorState error={daily.error} /> : dailyData.length > 0 ? <div className="chart-wrap" role="group" aria-label="Daily energy by local calendar day" data-testid="daily-chart" data-day-count={dailyData.length} data-day-source={intervalDailyAvailable ? 'bounded-intervals' : 'calendar-summaries'} data-unallocated-gap-count={intervalDailyResult.unallocated.length} data-selected-start-day={selectedStartDay} data-selected-end-day={selectedEndDay}><ResponsiveContainer width="100%" height="100%"><BarChart accessibilityLayer data={dailyData} margin={{ top: 16, right: 8, bottom: 8, left: 8 }}><CartesianGrid stroke="#33413c" strokeDasharray="3 4" vertical={false} /><XAxis dataKey="epoch" type="category" interval={0} tickFormatter={(value: number) => dailyTick(value, displayTimezone)} tick={{ fill: '#9ca9a4', fontSize: 11 }} axisLine={false} tickLine={false} /><YAxis tick={{ fill: '#9ca9a4', fontSize: 11 }} tickFormatter={energyAxis.tick} axisLine={false} tickLine={false} width={energyAxis.width} /><Tooltip content={<UsageTooltip timezone={displayTimezone} unit="kWh" />} wrapperStyle={{ outline: 'none' }} /><Bar name="Energy" dataKey="value" fill="#65d98b" radius={[5, 5, 0, 0]} maxBarSize={32} isAnimationActive={false} /></BarChart></ResponsiveContainer></div> : <EmptyState title="No saved energy for this range" detail="Missing calendar days remain missing; they are not shown as zero usage." />}
        {intervalDailyAvailable && intervalDailyResult.unallocated.length > 0 && <Notice kind="warning">{intervalDailyResult.unallocated.length} connection gap{intervalDailyResult.unallocated.length === 1 ? '' : 's'} cannot be assigned to one fully selected local day. {unallocatedKnownEnergy > 0 ? `${numeric(unallocatedKnownEnergy, 'kWh')} remains outside the bars and total.` : 'Its energy remains unknown and is not included in the bars or total.'}</Notice>}
        <div className="dashboard-energy-total"><span>{intervalDailyAvailable ? intervalDailyResult.unallocated.length > 0 ? 'Selected range assigned total' : 'Selected range total' : 'Local calendar-day total'}</span><strong>{numeric(dailyTotal === null || dailyTotal === undefined ? null : Number(dailyTotal), 'kWh')}</strong></div>
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
