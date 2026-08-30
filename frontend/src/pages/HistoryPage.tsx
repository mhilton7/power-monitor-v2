import { useQuery } from '@tanstack/react-query';
import { endOfDay, startOfDay, subDays, subHours } from 'date-fns';
import { CalendarRange, Download, Info, ZoomIn } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { Area, AreaChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { api } from '../api';
import { PermissionGate } from '../auth/PermissionGate';
import { ChartRangeSelector } from '../components/ChartRangeSelector';
import { Card, EmptyState, ErrorState, Loading, Notice } from '../components/ui';
import { useChartRangeSelection } from '../hooks/useChartRangeSelection';
import { browserTimezone, chartAxisFormat, chartTick, dateTime, download, inputDateTime, money, numeric, percent, resolveDisplayTimezone } from '../lib/format';
import { useHomeScope } from '../home/useHomeScope';
import { useHeartbeatTickerNow } from '../lib/heartbeatTicker';

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
const individualOnlyMetrics = new Set(['voltage', 'current', 'frequency', 'power_factor']);

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

function initialCustomRange(anchorMs: number) {
  return { from: inputDateTime(subHours(new Date(anchorMs), 24)), to: inputDateTime(new Date(anchorMs)) };
}

export function HistoryPage() {
  const browserNow = useHeartbeatTickerNow();
  const [rangeAnchor, setRangeAnchor] = useState(() => Date.now());
  const [liveQueryNow, setLiveQueryNow] = useState(() => Date.now());
  const [preset, setPreset] = useState<Preset>('24 hours');
  const presetWasChanged = useRef(false);
  const [metric, setMetric] = useState<(typeof metrics)[number]['value']>('power');
  const [timezoneOverride, setTimezoneOverride] = useState('');
  const [custom, setCustom] = useState(() => initialCustomRange(rangeAnchor));
  const [appliedCustom, setAppliedCustom] = useState(() => initialCustomRange(rangeAnchor));
  const [manualLiveDomain, setManualLiveDomain] = useState<{ startMs: number; endMs: number } | null>(null);
  const homeScope = useHomeScope();
  const { selectedHomeId } = homeScope;
  const preferences = useQuery({ queryKey: ['preferences'], queryFn: api.preferences });
  const devices = useQuery({ queryKey: ['devices', selectedHomeId], queryFn: () => api.devices(selectedHomeId), enabled: Boolean(selectedHomeId) });
  const circuits = useQuery({ queryKey: ['circuits', selectedHomeId], queryFn: () => api.circuits(selectedHomeId), enabled: Boolean(selectedHomeId) });
  const [deepLinkedScope, setDeepLinkedScope] = useState(() => {
    const query = new URLSearchParams(window.location.search);
    return query.get('scope') ?? (query.get('aggregate_circuit_id') ? `circuit:${query.get('aggregate_circuit_id')}` : query.get('device_id') ? `device:${query.get('device_id')}` : '');
  });
  const [selectedScope, setSelectedScope] = useState('');
  const verifiedCircuits = circuits.data?.circuits.filter((circuit) => circuit.aggregate_mode === 'verified_sum') ?? [];
  const scopeAvailable = (candidate: string) => candidate.startsWith('device:')
    ? Boolean(devices.data?.devices.some((device) => `device:${device.id}` === candidate))
    : candidate.startsWith('circuit:') && Boolean(circuits.data?.circuits.some((circuit) => `circuit:${circuit.id}` === candidate && circuit.aggregate_mode === 'verified_sum'));
  const storedScope = selectedHomeId ? sessionStorage.getItem(`powermeter:history-scope:${selectedHomeId}`) ?? '' : '';
  const designatedBranch = verifiedCircuits.find((circuit) => circuit.is_billing_source)
    ?? verifiedCircuits.find((circuit) => circuit.is_home_total || circuit.purpose === 'whole_home_total');
  const scopeValue = scopeAvailable(deepLinkedScope)
    ? deepLinkedScope
    : scopeAvailable(selectedScope)
      ? selectedScope
      : scopeAvailable(storedScope)
        ? storedScope
        : designatedBranch
          ? `circuit:${designatedBranch.id}`
          : devices.data?.devices[0]
            ? `device:${devices.data.devices[0].id}`
            : '';
  const [scopeKind, scopeId = ''] = scopeValue.split(':');
  const deviceId = scopeKind === 'device' ? scopeId : '';
  const circuitId = scopeKind === 'circuit' ? scopeId : '';
  const displayAnchor = preset === 'Live' ? browserNow : rangeAnchor;
  const displayRange = useMemo(() => preset === 'Custom'
    ? { from: new Date(appliedCustom.from), to: new Date(appliedCustom.to), aggregation: '5m' }
    : rangeFor(preset, new Date(displayAnchor)), [appliedCustom.from, appliedCustom.to, displayAnchor, preset]);
  const queryRange = useMemo(() => preset === 'Live'
    ? manualLiveDomain
      ? { from: new Date(manualLiveDomain.startMs), to: new Date(manualLiveDomain.endMs), aggregation: '1m' }
      : rangeFor(preset, new Date(liveQueryNow))
    : displayRange, [displayRange, liveQueryNow, manualLiveDomain, preset]);
  const params = useMemo(() => {
    const query = new URLSearchParams({ home_id: selectedHomeId, from: queryRange.from.toISOString(), to: queryRange.to.toISOString(), metric, resolution_seconds: queryRange.aggregation === '1m' ? '60' : queryRange.aggregation === '5m' ? '300' : queryRange.aggregation === '1h' ? '3600' : queryRange.aggregation === 'day' ? '86400' : '' });
    if (deviceId) query.set('device_id', deviceId);
    if (circuitId) query.set('aggregate_circuit_id', circuitId);
    return query;
  }, [circuitId, deviceId, metric, queryRange.aggregation, queryRange.from, queryRange.to, selectedHomeId]);
  const history = useQuery({
    queryKey: ['history', params.toString()],
    queryFn: () => api.history(params),
    enabled: Boolean(selectedHomeId && (deviceId || circuitId)) && queryRange.to > queryRange.from,
  });
  const baseOuterDomain = useMemo(() => ({
    startMs: displayRange.from.getTime(),
    endMs: displayRange.to.getTime(),
  }), [displayRange.from, displayRange.to]);
  const historyRangeIdentity = `${selectedHomeId}:${scopeValue}:${metric}:${preset}:${preset === 'Custom' ? `${appliedCustom.from}:${appliedCustom.to}` : rangeAnchor}`;
  const outerDomain = preset === 'Live' && manualLiveDomain ? manualLiveDomain : baseOuterDomain;
  const rangeSelection = useChartRangeSelection({
    identity: historyRangeIdentity,
    outerDomain,
    minimumDurationMs: Math.max(1_000, Number(history.data?.resolution_seconds ?? 60) * 1_000),
  });
  const metricDefinition = metrics.find((entry) => entry.value === metric) ?? metrics[0];
  const detectedBrowserTimezone = browserTimezone();
  const automaticTimezone = resolveDisplayTimezone(preferences.data?.display_timezone, history.data?.timezone);
  const timezone = timezoneOverride || automaticTimezone;
  const automaticTimezoneLabel = preferences.data?.display_timezone
    ? `Saved preference · ${automaticTimezone}`
    : detectedBrowserTimezone
      ? `Browser · ${automaticTimezone}`
      : history.data?.timezone
        ? `Home · ${automaticTimezone}`
        : `Fallback · ${automaticTimezone}`;
  const selectedRange = rangeSelection.selection;
  const rangeHours = (selectedRange.endMs - selectedRange.startMs) / 3_600_000;
  const timeAxisTicks = [selectedRange.startMs, selectedRange.endMs];
  const chartData = useMemo(() => {
    if (!history.data) return [];
    const points = history.data.points.map((point) => ({
      ...point,
      epoch: new Date(point.timestamp).getTime(),
      plottedValue: metric === 'cost' ? (point.cost === null ? null : Number(point.cost)) : point.value === null ? null : Number(point.value),
      gapBoundary: false,
    }));
    for (const gap of history.data.missing_ranges) {
      const start = Math.max(outerDomain.startMs, new Date(gap.start).getTime());
      const end = Math.min(outerDomain.endMs, new Date(gap.end).getTime());
      if (Number.isFinite(start) && Number.isFinite(end) && end - start > 2) {
        points.push({ timestamp: new Date(start + 1).toISOString(), value: null, cost: null, quality: null, epoch: start + 1, plottedValue: null, gapBoundary: true });
        points.push({ timestamp: new Date(end - 1).toISOString(), value: null, cost: null, quality: null, epoch: end - 1, plottedValue: null, gapBoundary: true });
      }
    }
    return points.sort((left, right) => left.epoch - right.epoch);
  }, [history.data, metric, outerDomain.endMs, outerDomain.startMs]);
  const visibleChartData = useMemo(() => chartData.filter((point) => point.epoch >= selectedRange.startMs && point.epoch <= selectedRange.endMs), [chartData, selectedRange.endMs, selectedRange.startMs]);
  const historyAxis = useMemo(() => chartAxisFormat(visibleChartData.map((point) => point.plottedValue), metricDefinition.unit, metric === 'cost' ? 62 : 58), [metric, metricDefinition.unit, visibleChartData]);
  const hasCommittedPoint = chartData.some((point) => point.plottedValue !== null);
  const hasVisibleCommittedPoint = visibleChartData.some((point) => point.plottedValue !== null);
  const lastSavedPoint = [...visibleChartData].reverse().find((point) => point.plottedValue !== null);
  const recoveredGapEnergy = history.data?.recovered_gap_energy_kwh === null || history.data?.recovered_gap_energy_kwh === undefined
    ? null
    : Number(history.data.recovered_gap_energy_kwh);

  useEffect(() => {
    if (!preferences.data || presetWasChanged.current) return;
    const preferred: Record<'day' | 'week' | 'month' | 'billing_cycle', Preset> = {
      day: '24 hours',
      week: '7 days',
      month: '30 days',
      billing_cycle: 'Billing cycle',
    };
    setRangeAnchor(Date.now());
    setPreset(preferred[preferences.data.history_range]);
  }, [preferences.data]);

  useEffect(() => {
    if (preset !== 'Live') return;
    const refresh = () => setLiveQueryNow(Date.now());
    refresh();
    const timer = window.setInterval(refresh, (preferences.data?.refresh_seconds ?? 15) * 1000);
    window.addEventListener('powermeter:measurement', refresh);
    return () => { window.clearInterval(timer); window.removeEventListener('powermeter:measurement', refresh); };
  }, [preferences.data?.refresh_seconds, preset]);

  function applyCustom(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    presetWasChanged.current = true;
    setAppliedCustom(custom);
    setRangeAnchor(browserNow);
    setManualLiveDomain(null);
    setPreset('Custom');
  }

  function choosePreset(next: Preset) {
    presetWasChanged.current = true;
    if (next === preset) return;
    setRangeAnchor(browserNow);
    setLiveQueryNow(browserNow);
    setManualLiveDomain(null);
    setPreset(next);
  }

  function beginManualRange(selection: { startMs: number; endMs: number }) {
    presetWasChanged.current = true;
    if (preset === 'Live') setManualLiveDomain(baseOuterDomain);
    rangeSelection.beginManual(selection);
  }

  function resetHistoryRange() {
    setManualLiveDomain(null);
    rangeSelection.reset();
  }

  function resumeLiveHistory() {
    resetHistoryRange();
    setLiveQueryNow(browserNow);
  }

  async function exportCsv() {
    const blob = await api.exportHistory(params);
    download(blob, `powermeter-history-${queryRange.from.toISOString().slice(0, 10)}-${queryRange.to.toISOString().slice(0, 10)}.csv`);
  }

  function chooseScope(next: string) {
    setManualLiveDomain(null);
    if (next.startsWith('circuit:') && individualOnlyMetrics.has(metric)) setMetric('power');
    setSelectedScope(next);
    setDeepLinkedScope(next);
    if (selectedHomeId) sessionStorage.setItem(`powermeter:history-scope:${selectedHomeId}`, next);
    const url = new URL(window.location.href);
    url.searchParams.set('scope', next);
    url.searchParams.delete('device_id');
    url.searchParams.delete('aggregate_circuit_id');
    window.history.replaceState(null, '', url);
  }

  function chooseMetric(next: typeof metric) {
    setManualLiveDomain(null);
    setMetric(next);
  }

  if (homeScope.isLoading) return <div className="page"><h1 className="sr-only">History</h1><Loading label="Loading authorized homes" /></div>;
  if (homeScope.isError) return <div className="page"><h1 className="sr-only">History</h1><ErrorState error={homeScope.error} retry={homeScope.refetch} /></div>;
  if (!selectedHomeId) return <div className="page"><h1 className="sr-only">History</h1><EmptyState title={homeScope.homeScopes.length === 0 ? 'No authorized home' : 'Choose an active home'} detail={homeScope.homeScopes.length === 0 ? 'Your account has no authorized home scope. History remains unavailable.' : 'Select a home from the Active home control before loading History.'} /></div>;
  if (devices.isLoading || circuits.isLoading) return <div className="page"><h1 className="sr-only">History</h1><Loading label="Loading sensors and service branches" /></div>;
  if (devices.isError || circuits.isError) return <div className="page"><h1 className="sr-only">History</h1><ErrorState error={devices.error ?? circuits.error} retry={() => { void devices.refetch(); void circuits.refetch(); }} /></div>;
  if (!scopeValue) return <div className="page"><h1 className="sr-only">History</h1><EmptyState title="No History source" detail="Add a sensor or service branch before viewing saved readings." /></div>;

  return <div className="page history-page">
    <header className="page-heading"><div><p className="eyebrow">Accepted sensor readings</p><h1>History</h1><p>Readings appear as measured. Zero stays zero, and times without a reading remain visible.</p></div><PermissionGate permission="history.export"><button type="button" className="button button-secondary" onClick={() => void exportCsv()} disabled={!history.data}><Download aria-hidden="true" /> Export CSV</button></PermissionGate></header>
    <Card className="history-controls">
      <div className="preset-tabs" role="group" aria-label="History range">{presets.map((entry) => <button type="button" key={entry} className={preset === entry ? 'active' : ''} aria-pressed={preset === entry} onClick={() => choosePreset(entry)}>{entry}</button>)}</div>
      <div className="filter-row">
        <div className="field"><label htmlFor="history-device">Service branch or sensor</label><select id="history-device" value={scopeValue} onChange={(event) => chooseScope(event.target.value)}>{verifiedCircuits.map((circuit) => <option key={circuit.id} value={`circuit:${circuit.id}`}>{circuit.name}{circuit.is_billing_source || circuit.is_home_total ? ' · Main service' : ''}</option>)}{devices.data?.devices.map((device) => <option key={device.id} value={`device:${device.id}`}>{device.friendly_name}</option>)}</select></div>
        <div className="field"><label htmlFor="history-metric">Metric</label><select id="history-metric" value={metric} onChange={(event) => chooseMetric(event.target.value as typeof metric)}>{metrics.map((entry) => <option key={entry.value} value={entry.value} disabled={Boolean(circuitId && individualOnlyMetrics.has(entry.value))}>{entry.label}{circuitId && individualOnlyMetrics.has(entry.value) ? ' · individual sensors only' : ''}</option>)}</select></div>
        <div className="field"><label htmlFor="history-timezone">Display timezone</label><select id="history-timezone" value={timezone} onChange={(event) => setTimezoneOverride(event.target.value)}><option value={automaticTimezone}>{automaticTimezoneLabel}</option>{automaticTimezone !== 'UTC' && <option value="UTC">UTC · technical view</option>}{history.data?.timezone && history.data.timezone !== automaticTimezone && history.data.timezone !== 'UTC' && <option value={history.data.timezone}>Home · {history.data.timezone}</option>}</select></div>
      </div>
      {preset === 'Custom' && <form className="custom-range" onSubmit={applyCustom}><div className="field"><label htmlFor="custom-from">From</label><input id="custom-from" type="datetime-local" value={custom.from} max={custom.to} onChange={(event) => setCustom((value) => ({ ...value, from: event.target.value }))} required /></div><div className="field"><label htmlFor="custom-to">To</label><input id="custom-to" type="datetime-local" value={custom.to} min={custom.from} max={inputDateTime(endOfDay(new Date(browserNow)))} onChange={(event) => setCustom((value) => ({ ...value, to: event.target.value }))} required /></div><button className="button button-primary" type="submit"><CalendarRange aria-hidden="true" /> Apply range</button></form>}
    </Card>

    {history.isLoading && <Loading label="Loading saved readings" />}
    {history.isError && <ErrorState error={history.error} retry={() => void history.refetch()} />}
    {history.data && <>
      <div className="history-summary-grid"><Card eyebrow="Loaded query" title="Energy"><strong className="stat-value">{numeric(history.data.energy_kwh === null ? null : Number(history.data.energy_kwh), 'kWh')}</strong><small>{dateTime(queryRange.from.toISOString(), timezone)} – {dateTime(queryRange.to.toISOString(), timezone)}</small></Card><Card eyebrow="Connection gaps" title="Recovered energy"><strong className="stat-value">{numeric(recoveredGapEnergy, 'kWh')}</strong><small>{recoveredGapEnergy && recoveredGapEnergy > 0 ? 'Recovered from the meter total' : 'No recovered gap energy reported'}</small></Card><Card eyebrow="Reading coverage" title="Accepted readings"><strong className="stat-value">{percent(history.data.completeness === null ? null : Number(history.data.completeness))}</strong><small>{history.data.missing_ranges.length > 0 ? 'Some readings are missing.' : 'No known gaps in this range'}</small></Card></div>
      <Notice>Showing readings for {deviceId ? devices.data?.devices.find((device) => device.id === deviceId)?.friendly_name ?? 'the selected sensor' : circuits.data?.circuits.find((circuit) => circuit.id === circuitId)?.name ?? 'the selected service branch'}. Sensors that measure the same electricity are never added together.</Notice>
      {recoveredGapEnergy !== null && recoveredGapEnergy > 0 && <Notice kind="info">Energy was recovered from the meter total, but the exact power pattern during the connection gap is unavailable.</Notice>}
      <Card title={`${metricDefinition.label} over time`} eyebrow={`${history.data.resolution_seconds}s aggregation · display ${timezone}`} action={<div className="history-chart-actions"><span className="chart-instruction"><ZoomIn aria-hidden="true" /> Drag either handle or the selected window</span>{rangeSelection.mode === 'manual' && <button type="button" className="text-button" onClick={resetHistoryRange}>Reset zoom</button>}{preset === 'Live' && rangeSelection.mode === 'manual' && <button type="button" className="text-button" onClick={resumeLiveHistory}>Resume live</button>}</div>} className="history-chart-card">
        {!hasCommittedPoint ? <EmptyState title="No readings were received during this time." detail="Choose another time range or check the sensor connection." /> : <div className="history-chart" data-testid="history-chart" data-missing-gap-style="unshaded" data-range-mode={rangeSelection.mode}>
          <div className="chart-range-layout">
            <div className="chart-range-plot">{hasVisibleCommittedPoint ? <ResponsiveContainer width="100%" height="100%">{metric === 'power' || metric === 'energy' || metric === 'cost' ? <AreaChart title={`${metricDefinition.label} over time`} desc="Accepted sensor readings. Times without a reading remain unshaded breaks in the line." data={visibleChartData} margin={{ top: 18, right: 18, bottom: 12, left: 8 }}><defs><linearGradient id="historyFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#65e692" stopOpacity={.5} /><stop offset="100%" stopColor="#65e692" stopOpacity={.02} /></linearGradient></defs><CartesianGrid stroke="#33413c" strokeDasharray="3 5" vertical={false} /><XAxis dataKey="epoch" type="number" domain={[selectedRange.startMs, selectedRange.endMs]} allowDataOverflow ticks={timeAxisTicks} scale="time" minTickGap={80} interval="preserveStartEnd" tickFormatter={(value: number) => chartTick(value, rangeHours, timezone)} tick={{ fill: '#aab5b1', fontSize: 12 }} axisLine={false} tickLine={false} /><YAxis tick={{ fill: '#aab5b1', fontSize: 12 }} tickFormatter={historyAxis.tick} axisLine={false} tickLine={false} width={historyAxis.width} /><Tooltip content={({ active, payload }) => {
              const point = payload?.[0]?.payload as (typeof visibleChartData)[number] | undefined;
              return active && point ? <div className="chart-tooltip"><strong>{dateTime(point.timestamp, timezone)}</strong><span>{metric === 'cost' ? money(point.cost) : numeric(point.value === null ? null : Number(point.value), metricDefinition.unit)}</span>{point.cost !== undefined && metric !== 'cost' && <span>Estimated cost: {money(point.cost)}</span>}<span>Reading coverage: {percent(point.quality === null ? null : Number(point.quality))}</span></div> : null;
            }} /><Area type="monotone" dataKey="plottedValue" stroke="#65e692" strokeWidth={2} fill="url(#historyFill)" connectNulls={false} isAnimationActive={false} /></AreaChart> : <LineChart title={`${metricDefinition.label} over time`} desc="Saved sensor readings. Times without a reading remain unshaded breaks in the line." data={visibleChartData} margin={{ top: 18, right: 18, bottom: 12, left: 8 }}><CartesianGrid stroke="#33413c" strokeDasharray="3 5" vertical={false} /><XAxis dataKey="epoch" type="number" domain={[selectedRange.startMs, selectedRange.endMs]} allowDataOverflow ticks={timeAxisTicks} scale="time" minTickGap={80} interval="preserveStartEnd" tickFormatter={(value: number) => chartTick(value, rangeHours, timezone)} tick={{ fill: '#aab5b1', fontSize: 12 }} axisLine={false} tickLine={false} /><YAxis tick={{ fill: '#aab5b1', fontSize: 12 }} tickFormatter={historyAxis.tick} axisLine={false} tickLine={false} width={historyAxis.width} /><Tooltip content={({ active, payload }) => {
              const point = payload?.[0]?.payload as (typeof visibleChartData)[number] | undefined;
              return active && point ? <div className="chart-tooltip"><strong>{dateTime(point.timestamp, timezone)}</strong><span>{numeric(point.value === null ? null : Number(point.value), metricDefinition.unit)}</span><span>Reading coverage: {percent(point.quality === null ? null : Number(point.quality))}</span></div> : null;
            }} /><Line type="monotone" dataKey="plottedValue" stroke="#65e692" strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false} /></LineChart>}</ResponsiveContainer> : <EmptyState title="No readings in the selected range" detail="Move or resize the range to view saved readings." />}</div>
            <ChartRangeSelector key={historyRangeIdentity} label="Saved History range" outerDomain={outerDomain} selection={selectedRange} mode={rangeSelection.mode} minimumDurationMs={Math.max(1_000, history.data.resolution_seconds * 1_000)} formatValue={(value) => dateTime(new Date(value).toISOString(), timezone)} onManualStart={beginManualRange} onCommit={rangeSelection.commitManual} testId="history-range" />
          </div>
        </div>}
        <p className="chart-selected-range" data-testid="history-selected-range" data-start-ms={selectedRange.startMs} data-end-ms={selectedRange.endMs}>{dateTime(new Date(selectedRange.startMs).toISOString(), timezone)} – {dateTime(new Date(selectedRange.endMs).toISOString(), timezone)}</p>
        {preset === 'Live' && <p className="disclosure" data-testid="live-timeline-status" data-view-end={selectedRange.endMs}>Timeline ends {dateTime(new Date(selectedRange.endMs).toISOString(), timezone)}. Last saved reading: {lastSavedPoint ? dateTime(lastSavedPoint.timestamp, timezone) : 'none in this range'}.</p>}
        <div className="chart-legend"><span><i className="legend-line" />Accepted reading</span><span><i className="legend-gap" />Missing reading</span><span><Info aria-hidden="true" /> A measured zero renders at zero; times without a reading form a gap.</span></div>
      </Card>
      {history.data.missing_ranges.length > 0 && <Card title="Some readings are missing."><div className="gap-list">{history.data.missing_ranges.map((gap) => <div key={`${gap.start}-${gap.end}`}><strong>{dateTime(gap.start, timezone)} – {dateTime(gap.end, timezone)}</strong><span>No reading was received during this time.</span></div>)}</div></Card>}
      {history.data.connection_gaps.length > 0 && <Card title="Connection gap details"><div className="gap-list">{history.data.connection_gaps.map((gap) => <div key={`${gap.device_id}:${gap.start_utc}:${gap.end_utc}`}><strong>{dateTime(gap.start_utc, timezone)} – {dateTime(gap.end_utc, timezone)}</strong><span>{gap.recovered_energy_kwh === null ? 'No recovered energy reported' : `${numeric(Number(gap.recovered_energy_kwh), 'kWh')} recovered`} · {gap.status.replaceAll('_', ' ')}</span></div>)}</div></Card>}
    </>}
  </div>;
}
