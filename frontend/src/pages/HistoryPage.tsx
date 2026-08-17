import { useQuery } from '@tanstack/react-query';
import { endOfDay, startOfDay, subDays, subHours } from 'date-fns';
import { CalendarRange, Download, Info, ZoomIn } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { Area, AreaChart, Brush, CartesianGrid, Line, LineChart, ReferenceArea, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { api } from '../api';
import { PermissionGate } from '../auth/PermissionGate';
import { Card, EmptyState, ErrorState, Loading, Notice } from '../components/ui';
import { chartTick, dateTime, download, inputDateTime, money, numeric, percent } from '../lib/format';
import { useHomeScope } from '../home/useHomeScope';

const presets = ['Live', 'Today', '24 hours', '7 days', '30 days', 'Billing cycle', 'Custom'] as const;
type Preset = typeof presets[number];

const metrics = [
  { value: 'power', label: 'Active power', unit: 'kW' },
  { value: 'voltage', label: 'Voltage', unit: 'V' },
  { value: 'current', label: 'Current', unit: 'A' },
  { value: 'frequency', label: 'Frequency', unit: 'Hz' },
  { value: 'power_factor', label: 'Power factor', unit: '' },
  { value: 'energy', label: 'Energy', unit: 'kWh' },
  { value: 'cost', label: 'Estimated cost', unit: '$' },
] as const;

function rangeFor(preset: Preset, now: Date): { from: Date; to: Date; aggregation: string } {
  switch (preset) {
    case 'Live': return { from: subHours(now, 1), to: now, aggregation: '1m' };
    case 'Today': return { from: startOfDay(now), to: now, aggregation: '5m' };
    case '24 hours': return { from: subHours(now, 24), to: now, aggregation: '5m' };
    case '7 days': return { from: subDays(now, 7), to: now, aggregation: '1h' };
    case '30 days': return { from: subDays(now, 30), to: now, aggregation: '1h' };
    case 'Billing cycle': return { from: subDays(now, 30), to: now, aggregation: '1h' };
    case 'Custom': return { from: subHours(now, 24), to: now, aggregation: '5m' };
  }
}

export function HistoryPage() {
  const [now] = useState(() => new Date());
  const [preset, setPreset] = useState<Preset>('24 hours');
  const presetWasChanged = useRef(false);
  const [metric, setMetric] = useState<(typeof metrics)[number]['value']>('power');
  const [timezone, setTimezone] = useState('America/Los_Angeles');
  const [custom, setCustom] = useState(() => ({ from: inputDateTime(subHours(now, 24)), to: inputDateTime(now) }));
  const homeScope = useHomeScope();
  const { selectedHomeId } = homeScope;
  const preferences = useQuery({ queryKey: ['preferences'], queryFn: api.preferences });
  const devices = useQuery({ queryKey: ['devices', selectedHomeId], queryFn: () => api.devices(selectedHomeId), enabled: Boolean(selectedHomeId) });
  const circuits = useQuery({ queryKey: ['circuits', selectedHomeId], queryFn: () => api.circuits(selectedHomeId), enabled: Boolean(selectedHomeId) });
  const [selectedScope, setSelectedScope] = useState('');
  const verifiedCircuits = circuits.data?.circuits.filter((circuit) => circuit.aggregate_mode === 'verified_sum') ?? [];
  const selectedScopeAvailable = selectedScope.startsWith('device:')
    ? devices.data?.devices.some((device) => `device:${device.id}` === selectedScope)
    : selectedScope.startsWith('circuit:') && circuits.data?.circuits.some((circuit) => `circuit:${circuit.id}` === selectedScope && circuit.aggregate_mode === 'verified_sum');
  const scopeValue = selectedScopeAvailable
    ? selectedScope
    : verifiedCircuits.length === 1
      ? `circuit:${verifiedCircuits[0]!.id}`
      : devices.data?.devices[0]
        ? `device:${devices.data.devices[0].id}`
        : '';
  const [scopeKind, scopeId = ''] = scopeValue.split(':');
  const deviceId = scopeKind === 'device' ? scopeId : '';
  const circuitId = scopeKind === 'circuit' ? scopeId : '';
  const range = preset === 'Custom'
    ? { from: new Date(custom.from), to: new Date(custom.to), aggregation: '5m' }
    : rangeFor(preset, now);
  const params = useMemo(() => {
    const query = new URLSearchParams({ home_id: selectedHomeId, from: range.from.toISOString(), to: range.to.toISOString(), metric, resolution_seconds: range.aggregation === '1m' ? '60' : range.aggregation === '5m' ? '300' : range.aggregation === '1h' ? '3600' : range.aggregation === 'day' ? '86400' : '' });
    if (deviceId) query.set('device_id', deviceId);
    if (circuitId) query.set('aggregate_circuit_id', circuitId);
    return query;
  }, [circuitId, deviceId, metric, range.aggregation, range.from, range.to, selectedHomeId]);
  const history = useQuery({
    queryKey: ['history', params.toString()],
    queryFn: () => api.history(params),
    enabled: Boolean(selectedHomeId && (deviceId || circuitId)) && range.to > range.from,
    refetchInterval: preset === 'Live' ? (preferences.data?.refresh_seconds ?? 15) * 1000 : false,
  });
  const metricDefinition = metrics.find((entry) => entry.value === metric) ?? metrics[0];
  const rangeHours = (range.to.getTime() - range.from.getTime()) / 3_600_000;
  const chartData = useMemo(() => history.data?.points.map((point) => ({
    ...point,
    epoch: new Date(point.timestamp).getTime(),
    plottedValue: metric === 'cost' ? (point.cost === null ? null : Number(point.cost)) : point.value === null ? null : Number(point.value),
  })) ?? [], [history.data, metric]);
  const hasCommittedPoint = chartData.some((point) => point.plottedValue !== null);
  const scopedDevices = circuitId
    ? devices.data?.devices.filter((device) => device.circuit_id === circuitId && device.include_in_aggregate) ?? []
    : devices.data?.devices.filter((device) => device.id === deviceId) ?? [];
  const pendingBacklog = scopedDevices.reduce((total, device) => total + (device.backlog ?? 0), 0);

  useEffect(() => {
    if (!preferences.data || presetWasChanged.current) return;
    const preferred: Record<'day' | 'week' | 'month' | 'billing_cycle', Preset> = {
      day: '24 hours',
      week: '7 days',
      month: '30 days',
      billing_cycle: 'Billing cycle',
    };
    setPreset(preferred[preferences.data.history_range]);
  }, [preferences.data]);

  function applyCustom(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    presetWasChanged.current = true;
    setPreset('Custom');
  }

  async function exportCsv() {
    const blob = await api.exportHistory(params);
    download(blob, `powermeter-history-${range.from.toISOString().slice(0, 10)}-${range.to.toISOString().slice(0, 10)}.csv`);
  }

  if (homeScope.isLoading) return <div className="page"><h1 className="sr-only">History</h1><Loading label="Loading authorized homes" /></div>;
  if (homeScope.isError) return <div className="page"><h1 className="sr-only">History</h1><ErrorState error={homeScope.error} retry={homeScope.refetch} /></div>;
  if (!selectedHomeId) return <div className="page"><h1 className="sr-only">History</h1><EmptyState title={homeScope.homeScopes.length === 0 ? 'No authorized home' : 'Choose an active home'} detail={homeScope.homeScopes.length === 0 ? 'Your account has no authorized home scope. History remains unavailable.' : 'Select a home from the Active home control before loading History.'} /></div>;
  if (devices.isLoading || circuits.isLoading) return <div className="page"><h1 className="sr-only">History</h1><Loading label="Loading sensor and aggregate scopes" /></div>;
  if (devices.isError || circuits.isError) return <div className="page"><h1 className="sr-only">History</h1><ErrorState error={devices.error ?? circuits.error} retry={() => { void devices.refetch(); void circuits.refetch(); }} /></div>;
  if (!scopeValue) return <div className="page"><h1 className="sr-only">History</h1><EmptyState title="No History scope" detail="This home has no enrolled sensor or verified aggregate. History remains empty instead of guessing a scope." /></div>;

  return <div className="page history-page">
    <header className="page-heading"><div><p className="eyebrow">Committed sensor evidence</p><h1>History</h1><p>Every point comes from an accepted durable PZEM interval. Missing data stays visibly missing.</p></div><PermissionGate permission="history.export"><button type="button" className="button button-secondary" onClick={() => void exportCsv()} disabled={!history.data}><Download aria-hidden="true" /> Export CSV</button></PermissionGate></header>
    <Card className="history-controls">
      <div className="preset-tabs" role="group" aria-label="History range">{presets.map((entry) => <button type="button" key={entry} className={preset === entry ? 'active' : ''} aria-pressed={preset === entry} onClick={() => { presetWasChanged.current = true; setPreset(entry); }}>{entry}</button>)}</div>
      <div className="filter-row">
        <div className="field"><label htmlFor="history-device">Sensor or aggregate scope</label><select id="history-device" value={scopeValue} onChange={(event) => setSelectedScope(event.target.value)}>{devices.data?.devices.map((device) => <option key={device.id} value={`device:${device.id}`}>{device.friendly_name}</option>)}{circuits.data?.circuits.filter((circuit) => circuit.aggregate_mode === 'verified_sum').map((circuit) => <option key={circuit.id} value={`circuit:${circuit.id}`}>{circuit.name} · verified aggregate</option>)}</select></div>
        <div className="field"><label htmlFor="history-metric">Metric</label><select id="history-metric" value={metric} onChange={(event) => setMetric(event.target.value as typeof metric)}>{metrics.map((entry) => <option key={entry.value} value={entry.value}>{entry.label}</option>)}</select></div>
        <div className="field"><label htmlFor="history-timezone">Timezone display</label><select id="history-timezone" value={timezone} onChange={(event) => setTimezone(event.target.value)}><option value="America/Los_Angeles">Home · America/Los_Angeles</option><option value="UTC">UTC</option></select></div>
      </div>
      {preset === 'Custom' && <form className="custom-range" onSubmit={applyCustom}><div className="field"><label htmlFor="custom-from">From</label><input id="custom-from" type="datetime-local" value={custom.from} max={custom.to} onChange={(event) => setCustom((value) => ({ ...value, from: event.target.value }))} required /></div><div className="field"><label htmlFor="custom-to">To</label><input id="custom-to" type="datetime-local" value={custom.to} min={custom.from} max={inputDateTime(endOfDay(now))} onChange={(event) => setCustom((value) => ({ ...value, to: event.target.value }))} required /></div><button className="button button-primary" type="submit"><CalendarRange aria-hidden="true" /> Apply range</button></form>}
    </Card>

    {history.isLoading && <Loading label="Loading committed History" />}
    {history.isError && <ErrorState error={history.error} retry={() => void history.refetch()} />}
    {history.data && <>
      <div className="history-summary-grid"><Card eyebrow="Selected range" title="Energy"><strong className="stat-value">{numeric(history.data.energy_kwh === null ? null : Number(history.data.energy_kwh), 'kWh')}</strong><small>{dateTime(range.from.toISOString(), timezone)} – {dateTime(range.to.toISOString(), timezone)}</small></Card><Card eyebrow="Selected range" title="Estimated cost"><strong className="stat-value">{money(history.data.cost)}</strong><small>Published rate assignment used by the server</small></Card><Card eyebrow="Data quality" title="Completeness"><strong className="stat-value">{percent(history.data.completeness === null ? null : Number(history.data.completeness))}</strong><small>{history.data.missing_ranges.length} missing range{history.data.missing_ranges.length === 1 ? '' : 's'}</small></Card></div>
      <Notice><strong>Estimate boundary:</strong> monitored scope is {deviceId ? devices.data?.devices.find((device) => device.id === deviceId)?.friendly_name ?? 'the selected sensor' : circuits.data?.circuits.find((circuit) => circuit.id === circuitId)?.name ?? 'the selected verified aggregate'}; usage comes only from authenticated sensor intervals. Aggregate choices appear only after an operator verifies non-overlapping meters. Fixed charges, baseline credits, CCA status and the immutable rate version determine which reusable pricing components are included. Results may differ from a utility bill because of meter accuracy, unmonitored loads, rate changes, taxes, credits, rounding and utility adjustments.</Notice>
      {!hasCommittedPoint && pendingBacklog > 0 && <Notice><strong>History is waiting for server acknowledgement:</strong> {pendingBacklog.toLocaleString()} authenticated interval{pendingBacklog === 1 ? '' : 's'} remain queued on the selected sensor scope. Live heartbeat power is intentionally not copied into durable History; points appear after those intervals are accepted and normalized.</Notice>}
      <Card title={`${metricDefinition.label} over time`} eyebrow={`${history.data.resolution_seconds}s aggregation · display ${timezone}`} action={<span className="chart-instruction"><ZoomIn aria-hidden="true" /> Drag the handles to zoom</span>} className="history-chart-card">
        {!hasCommittedPoint ? <EmptyState title={`No committed ${metricDefinition.label.toLowerCase()}`} detail={pendingBacklog > 0 ? `${pendingBacklog.toLocaleString()} authenticated intervals remain queued on this scope. Live readings are not substituted for missing durable History.` : 'No accepted value for this metric exists in the selected range. Missing data was not converted to zero.'} /> : <div className="history-chart" data-testid="history-chart"><ResponsiveContainer width="100%" height="100%">{metric === 'power' || metric === 'energy' || metric === 'cost' ? <AreaChart title={`${metricDefinition.label} over time`} desc="Authenticated committed sensor intervals. Missing readings render as gaps." data={chartData} margin={{ top: 18, right: 18, bottom: 12, left: 2 }}><defs><linearGradient id="historyFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#65e692" stopOpacity={.5} /><stop offset="100%" stopColor="#65e692" stopOpacity={.02} /></linearGradient></defs><CartesianGrid stroke="#33413c" strokeDasharray="3 5" vertical={false} />{history.data.missing_ranges.map((gap) => <ReferenceArea key={`${gap.start}-${gap.end}`} x1={new Date(gap.start).getTime()} x2={new Date(gap.end).getTime()} fill="#f6b94d" fillOpacity={.12} />)}<XAxis dataKey="epoch" type="number" domain={['dataMin', 'dataMax']} scale="time" minTickGap={80} interval="preserveStartEnd" tickFormatter={(value: number) => chartTick(value, rangeHours, timezone)} tick={{ fill: '#aab5b1', fontSize: 12 }} axisLine={false} tickLine={false} /><YAxis tick={{ fill: '#aab5b1', fontSize: 12 }} axisLine={false} tickLine={false} width={58} unit={metric === 'cost' ? '$' : metricDefinition.unit ? ` ${metricDefinition.unit}` : ''} /><Tooltip content={({ active, payload }) => {
          const point = payload?.[0]?.payload as (typeof chartData)[number] | undefined;
          return active && point ? <div className="chart-tooltip"><strong>{dateTime(point.timestamp, timezone)}</strong><span>{metric === 'cost' ? money(point.cost) : numeric(point.value === null ? null : Number(point.value), metricDefinition.unit)}</span>{point.cost !== undefined && metric !== 'cost' && <span>Estimated cost: {money(point.cost)}</span>}<span>Completeness: {percent(point.quality === null ? null : Number(point.quality))}</span></div> : null;
        }} /><Area type="monotone" dataKey="plottedValue" stroke="#65e692" strokeWidth={2} fill="url(#historyFill)" connectNulls={false} isAnimationActive={false} /><Brush ariaLabel="Zoom committed History" dataKey="epoch" height={28} travellerWidth={24} stroke="#65e692" fill="#141b19" tickFormatter={() => ''} /></AreaChart> : <LineChart title={`${metricDefinition.label} over time`} desc="Authenticated committed sensor intervals. Missing readings render as gaps." data={chartData} margin={{ top: 18, right: 18, bottom: 12, left: 2 }}><CartesianGrid stroke="#33413c" strokeDasharray="3 5" vertical={false} /><XAxis dataKey="epoch" type="number" domain={['dataMin', 'dataMax']} scale="time" minTickGap={80} interval="preserveStartEnd" tickFormatter={(value: number) => chartTick(value, rangeHours, timezone)} tick={{ fill: '#aab5b1', fontSize: 12 }} axisLine={false} tickLine={false} /><YAxis tick={{ fill: '#aab5b1', fontSize: 12 }} axisLine={false} tickLine={false} width={58} unit={metricDefinition.unit ? ` ${metricDefinition.unit}` : ''} /><Tooltip /><Line type="monotone" dataKey="plottedValue" stroke="#65e692" strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false} /><Brush ariaLabel="Zoom committed History" dataKey="epoch" height={28} travellerWidth={24} stroke="#65e692" fill="#141b19" tickFormatter={() => ''} /></LineChart>}</ResponsiveContainer></div>}
        <div className="chart-legend"><span><i className="legend-line" />Committed value</span><span><i className="legend-gap" />Missing interval</span><span><Info aria-hidden="true" /> A measured zero renders at zero; unavailable values form a gap.</span></div>
      </Card>
      {history.data.missing_ranges.length > 0 && <Card title="Missing-data evidence"><div className="gap-list">{history.data.missing_ranges.map((gap) => <div key={`${gap.start}-${gap.end}`}><strong>{dateTime(gap.start, timezone)} – {dateTime(gap.end, timezone)}</strong><span>Authenticated sensor evidence unavailable</span></div>)}</div></Card>}
    </>}
  </div>;
}
