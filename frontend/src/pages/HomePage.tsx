import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, ArrowRight, CalendarDays, CircleDollarSign, Clock3, Gauge, HardDrive, RefreshCw, RotateCcw, Sigma, UploadCloud, Waves, Zap } from 'lucide-react';
import { useMemo, useState, type ReactNode } from 'react';
import { Area, AreaChart, Bar, BarChart, Brush, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { api } from '../api';
import type { DeviceDetail } from '../api/schemas';
import { PermissionGate } from '../auth/PermissionGate';
import { SensorDrawer } from '../components/SensorDrawer';
import { Card, ConfirmDialog, EmptyState, ErrorState, Loading, Notice, StatusPill } from '../components/ui';
import { chartTick, dateTime, money, numeric, percent, timeAgo } from '../lib/format';
import { useHomeScope } from '../home/useHomeScope';

function historyParams(homeId: string, deviceId: string, from: Date, to: Date, metric: string, resolutionSeconds?: number) {
  const query = new URLSearchParams({ home_id: homeId, from: from.toISOString(), to: to.toISOString(), metric, device_id: deviceId });
  if (resolutionSeconds) query.set('resolution_seconds', String(resolutionSeconds));
  return query;
}

function PowerGauge({ watts, maxWatts = 10_000 }: { watts: number | null | undefined; maxWatts?: number }) {
  const ratio = watts === null || watts === undefined ? 0 : Math.min(1, Math.max(0, watts / maxWatts));
  const dash = ratio * 220;
  return <div className="power-gauge" aria-label={watts === null || watts === undefined ? 'Power gauge unavailable' : `Power gauge ${Math.round(ratio * 100)} percent of ${numeric(maxWatts / 1000, 'kilowatts', 0)}`}>
    <svg viewBox="0 0 220 140" role="img" aria-hidden="true"><path d="M25 120 A88 88 0 0 1 195 120" className="gauge-track" pathLength="220" /><path d="M25 120 A88 88 0 0 1 195 120" className="gauge-value" pathLength="220" strokeDasharray={`${dash} 220`} /><Zap x="90" y="47" width="40" height="40" /></svg>
    <div><span>0 kW</span><span>{maxWatts / 1000} kW</span></div>
  </div>;
}

function MetricCard({ icon, label, value, state = 'Normal' }: { icon: ReactNode; label: string; value: string; state?: string }) {
  return <Card className="metric-card"><div className="metric-heading"><span className="metric-icon">{icon}</span><div><span>{label}</span><strong>{value}</strong></div></div><small><span className="metric-ok" aria-hidden="true" />{state}</small></Card>;
}

function SummaryCard({ icon, label, value, detail }: { icon: ReactNode; label: string; value: string; detail: string }) {
  return <Card className="summary-card"><div className="summary-label">{icon}<span>{label}</span></div><strong>{value}</strong><small>{detail}</small></Card>;
}

function UsageTooltip({ active, payload, timezone }: { active?: boolean; payload?: Array<{ payload?: { timestamp: string; value: string | number | null; cost: string | number | null; quality: string | number | null } }>; timezone: string }) {
  const point = payload?.[0]?.payload;
  if (!active || !point) return null;
  return <div className="chart-tooltip"><strong>{dateTime(point.timestamp, timezone)}</strong><span>{numeric(point.value === null ? null : Number(point.value), 'kW', 2)}</span><span>Estimated cost: {money(point.cost)}</span><span>Completeness: {percent(point.quality === null ? null : Number(point.quality))}</span></div>;
}

export function HomePage() {
  const [now] = useState(() => new Date());
  const [selectedDevice, setSelectedDevice] = useState<DeviceDetail>();
  const [pendingCommand, setPendingCommand] = useState<'reboot' | 'format_storage_prepare' | 'format_storage_commit' | null>(null);
  const [formatEvidence, setFormatEvidence] = useState<{ token: string; prepareCommandId: string } | null>(null);
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
  const alerts = useQuery({ queryKey: ['alerts'], queryFn: api.alerts, refetchInterval: refreshInterval });
  const deviceId = home.data?.devices[0]?.id ?? '';
  const history24 = useQuery({
    queryKey: ['history', selectedHomeId, 'home-dashboard', dashboardDays, deviceId],
    queryFn: () => api.history(historyParams(selectedHomeId, deviceId, new Date(now.getTime() - dashboardDays * 24 * 60 * 60 * 1000), now, 'power', dashboardDays === 1 ? 300 : 3600)),
    enabled: Boolean(selectedHomeId && deviceId),
  });
  const daily = useQuery({
    queryKey: ['history', selectedHomeId, 'home-daily', dashboardDays, deviceId],
    queryFn: () => api.history(historyParams(selectedHomeId, deviceId, new Date(now.getTime() - dashboardDays * 24 * 60 * 60 * 1000), now, 'energy', 86400)),
    enabled: Boolean(selectedHomeId && deviceId),
  });
  const command = useMutation({
    mutationFn: ({ type, payload, prepare, typedConfirmation }: { type: string; payload?: Record<string, unknown>; prepare?: { commandId: string; confirmationToken: string }; typedConfirmation?: string }) => api.command(deviceId, type, payload, prepare ? { ...prepare, typedConfirmation: typedConfirmation ?? '' } : undefined),
    onSuccess: (result, variables) => {
      if (variables.type === 'format_storage_prepare' && result.confirmation_token) {
        setFormatEvidence({ token: result.confirmation_token, prepareCommandId: result.command.id });
        setPendingCommand(null);
      } else {
        setPendingCommand(null);
        setFormatEvidence(null);
      }
      void queryClient.invalidateQueries({ queryKey: ['devices'] });
    },
  });

  const primary = home.data?.devices[0];
  const primaryDetail = devices.data?.devices.find((device) => device.id === deviceId);
  const selectedDeviceCurrent = selectedDevice
    ? devices.data?.devices.find((device) => device.id === selectedDevice.id) ?? selectedDevice
    : undefined;
  const measurement = primary?.measurement;
  const liveKilowatts = measurement?.active_power_w === null || measurement?.active_power_w === undefined ? null : measurement.active_power_w / 1000;
  const chartData = useMemo(() => history24.data?.points.map((point) => ({ ...point, epoch: new Date(point.timestamp).getTime(), valueKw: point.value === null ? null : Number(point.value) })) ?? [], [history24.data]);
  const dailyData = useMemo(() => daily.data?.points.map((point) => ({ ...point, epoch: new Date(point.timestamp).getTime(), value: point.value === null ? null : Number(point.value) })) ?? [], [daily.data]);

  const formatCommitReady = Boolean(
    formatEvidence
    && primaryDetail?.last_command?.id === formatEvidence.prepareCommandId
    && primaryDetail.last_command.state === 'succeeded'
    && primaryDetail.last_command.result_evidence?.ready === true,
  );
  const actionableCommand = pendingCommand ?? (formatCommitReady ? 'format_storage_commit' : null);

  if (homeScope.isLoading) return <div className="page"><h1 className="sr-only">Home</h1><Loading label="Loading authorized homes" /></div>;
  if (homeScope.isError) return <div className="page"><h1 className="sr-only">Home</h1><ErrorState error={homeScope.error} retry={homeScope.refetch} /></div>;
  if (!selectedHomeId) return <div className="page"><h1 className="sr-only">Home</h1><EmptyState title={homeScope.homeScopes.length === 0 ? 'No authorized home' : 'Choose an active home'} detail={homeScope.homeScopes.length === 0 ? 'Your account has no authorized home scope. Home-specific data remains unavailable.' : 'Select a home from the Active home control before loading measurements.'} /></div>;
  if (home.isLoading) return <div className="page"><h1 className="sr-only">Home</h1><Loading label="Loading authenticated measurements" /></div>;
  if (home.isError) return <div className="page"><h1 className="sr-only">Home</h1><ErrorState error={home.error} retry={() => void home.refetch()} /></div>;
  const data = home.data;
  if (!data) return <div className="page"><h1 className="sr-only">Home</h1><ErrorState error={new Error('The Home response was empty.')} retry={() => void home.refetch()} /></div>;
  if (!primary) return <div className="page"><h1 className="sr-only">Home</h1><EmptyState title="No enrolled sensor" detail="Ask an administrator to create an enrollment token, then provision a headless sensor over USB." /></div>;
  const formatResult = primaryDetail?.last_command?.result_evidence;
  const formatImpact = formatResult?.ready === true
    ? `The sensor authenticated its prepare result: ${typeof formatResult.acknowledged_records_lost === 'number' ? formatResult.acknowledged_records_lost : 0} acknowledged and ${typeof formatResult.unacknowledged_records_lost === 'number' ? formatResult.unacknowledged_records_lost : 0} unacknowledged stored records will be removed. Identity, credentials, configuration, sequence floor and acknowledgement remain preserved.`
    : 'Authenticated storage-impact evidence is not available; commit remains disabled.';

  return <div className="home-dashboard">
    <h1 className="sr-only">Home</h1>
    {command.isSuccess && <Notice kind="success">Command {command.data.command.id} is queued. Awaiting authenticated device progress.</Notice>}
    {formatEvidence && !formatCommitReady && <Notice>Storage-format prepare {formatEvidence.prepareCommandId} is queued. Commit remains unavailable until authenticated readiness and loss-count evidence arrives.</Notice>}
    <section className="dashboard-top" aria-label="Live electrical overview">
      {visibleCards.has('live_power') && <><Card className="live-hero">
        <div className="hero-copy"><div className="hero-title"><h2>Live Power Usage</h2><StatusPill state={primary.state} {...(primary.state === 'live' ? { label: 'Live' } : {})} /></div><div className="hero-value" aria-label={numeric(liveKilowatts, 'kilowatts')}><strong>{liveKilowatts === null ? '—' : liveKilowatts.toFixed(2)}</strong><span>kW</span></div><p><Waves aria-hidden="true" /> {measurement?.measured_at ? `Updated ${timeAgo(measurement.measured_at)}` : 'Waiting for an authenticated heartbeat'}</p><small className="source-label">Live heartbeat measurement · not yet committed History</small></div>
        <PowerGauge watts={measurement?.active_power_w} />
      </Card>
      <div className="metric-grid">
        <MetricCard icon={<Activity aria-hidden="true" />} label="Voltage" value={numeric(measurement?.voltage_v, 'V', 1)} state={measurement?.voltage_v === null || measurement?.voltage_v === undefined ? 'Unavailable' : 'Normal'} />
        <MetricCard icon={<Gauge aria-hidden="true" />} label="Current" value={numeric(measurement?.current_a, 'A', 2)} state={measurement?.current_a === null || measurement?.current_a === undefined ? 'Unavailable' : 'Normal'} />
        <MetricCard icon={<Waves aria-hidden="true" />} label="Frequency" value={numeric(measurement?.frequency_hz, 'Hz', 2)} state={measurement?.frequency_hz === null || measurement?.frequency_hz === undefined ? 'Unavailable' : 'Normal'} />
        <MetricCard icon={<Sigma aria-hidden="true" />} label="Power Factor" value={numeric(measurement?.power_factor, '', 2)} state={measurement?.power_factor === null || measurement?.power_factor === undefined ? 'Unavailable' : measurement.power_factor >= .9 ? 'Good' : 'Needs attention'} />
      </div></>}
      {(visibleCards.has('energy') || visibleCards.has('cost') || visibleCards.has('completeness')) && <div className="summary-grid">
        {visibleCards.has('energy') && <SummaryCard icon={<Zap aria-hidden="true" />} label="Today Energy" value={numeric(data.summaries.today.energy_kwh === null ? null : Number(data.summaries.today.energy_kwh), 'kWh')} detail="Authenticated sensor intervals" />}
        {visibleCards.has('cost') && <SummaryCard icon={<CircleDollarSign aria-hidden="true" />} label="Today Est. Cost" value={money(data.summaries.today.cost)} detail="Sensor intervals · selected rate" />}
        {visibleCards.has('cost') && <SummaryCard icon={<CalendarDays aria-hidden="true" />} label="This Week Cost" value={money(data.summaries.week.cost)} detail={`${numeric(data.summaries.week.energy_kwh === null ? null : Number(data.summaries.week.energy_kwh), 'kWh')} monitored`} />}
        {visibleCards.has('completeness') && <SummaryCard icon={<Sigma aria-hidden="true" />} label="Today Completeness" value={percent(data.summaries.today.completeness === null ? null : Number(data.summaries.today.completeness))} detail="Missing readings remain missing" />}
      </div>}
    </section>

    {data.devices.length > 1 && <section className="multi-sensor-grid" aria-label="Individual sensor live cards">{data.devices.map((sensor) => {
      const detail = devices.data?.devices.find((entry) => entry.id === sensor.id);
      const watts = sensor.measurement?.active_power_w;
      return <Card key={sensor.id} className="individual-sensor-card"><button type="button" onClick={() => setSelectedDevice(detail)} disabled={!detail} aria-label={`Open ${sensor.friendly_name} sensor details`}><div><strong>{sensor.friendly_name}</strong><small>Authenticated heartbeat · {timeAgo(sensor.heartbeat_at)}</small></div><span>{watts === null || watts === undefined ? 'Not available' : numeric(watts / 1000, 'kW', 2)}</span><StatusPill state={sensor.state} /></button></Card>;
    })}<Notice>These cards are individual live sensor scopes. The dashboard never blindly sums parent and child meters; only an explicitly verified non-overlapping circuit may be aggregated.</Notice></section>}

    <div className="dashboard-lower">
    <section className="dashboard-middle" aria-label="Committed usage charts and rate">
      {(visibleCards.has('live_power') || visibleCards.has('completeness')) && <Card title={`Power History – ${dashboardRangeLabel}`} eyebrow="Committed sensor intervals" action={<span className="select-chip">kW</span>} className="usage-chart-card">
        {history24.isLoading ? <Loading label="Loading committed intervals" /> : history24.isError ? <ErrorState error={history24.error} /> : history24.data ? <div className="chart-wrap" data-testid="usage-chart"><ResponsiveContainer width="100%" height="100%"><AreaChart title={`Committed power over ${dashboardRangeLabel.toLowerCase()}`} desc="Authenticated sensor power intervals. Missing readings render as gaps." data={chartData} margin={{ top: 16, right: 12, bottom: 8, left: -12 }}><defs><linearGradient id="powerFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#65e692" stopOpacity={0.55} /><stop offset="100%" stopColor="#65e692" stopOpacity={0.02} /></linearGradient></defs><CartesianGrid stroke="#33413c" strokeDasharray="3 4" vertical={false} /><XAxis dataKey="epoch" type="number" domain={['dataMin', 'dataMax']} scale="time" minTickGap={70} interval="preserveStartEnd" tickFormatter={(value: number) => chartTick(value, dashboardDays * 24, history24.data?.timezone ?? 'America/Los_Angeles')} tick={{ fill: '#9ca9a4', fontSize: 11 }} axisLine={false} tickLine={false} /><YAxis tick={{ fill: '#9ca9a4', fontSize: 11 }} axisLine={false} tickLine={false} width={42} unit=" kW" /><Tooltip content={<UsageTooltip timezone={history24.data.timezone} />} /><Area type="monotone" dataKey="valueKw" stroke="#65e692" strokeWidth={2} fill="url(#powerFill)" connectNulls={false} isAnimationActive={false} /><Brush ariaLabel="Zoom committed power History" dataKey="epoch" height={28} travellerWidth={24} stroke="#65e692" fill="#151d1a" tickFormatter={() => ''} /></AreaChart></ResponsiveContainer></div> : null}
        <div className="chart-footer"><Clock3 aria-hidden="true" /><span>{dateTime(new Date(now.getTime() - dashboardDays * 86_400_000).toISOString(), history24.data?.timezone)} – {dateTime(now.toISOString(), history24.data?.timezone)}</span>{history24.data && <span>{percent(history24.data.completeness === null ? null : Number(history24.data.completeness))} complete · {history24.data.missing_ranges.length} gap{history24.data.missing_ranges.length === 1 ? '' : 's'}</span>}</div>
      </Card>}
      {visibleCards.has('energy') && <Card title="Daily Energy (kWh)" eyebrow="Committed, non-overlapping totals" action={<span className="select-chip">{dashboardRangeLabel}</span>} className="daily-chart-card">
        {daily.isLoading ? <Loading label="Loading daily energy" /> : daily.isError ? <ErrorState error={daily.error} /> : daily.data ? <div className="chart-wrap" data-testid="daily-chart"><ResponsiveContainer width="100%" height="100%"><BarChart title={`Committed daily energy over ${dashboardRangeLabel.toLowerCase()}`} desc="Authenticated non-overlapping daily sensor energy totals." data={dailyData} margin={{ top: 16, right: 8, bottom: 8, left: -16 }}><CartesianGrid stroke="#33413c" strokeDasharray="3 4" vertical={false} /><XAxis dataKey="epoch" type="number" domain={['dataMin', 'dataMax']} scale="time" minTickGap={45} tickFormatter={(value: number) => chartTick(value, dashboardDays * 24, daily.data?.timezone ?? 'America/Los_Angeles')} tick={{ fill: '#9ca9a4', fontSize: 11 }} axisLine={false} tickLine={false} /><YAxis tick={{ fill: '#9ca9a4', fontSize: 11 }} axisLine={false} tickLine={false} /><Tooltip content={<UsageTooltip timezone={daily.data.timezone} />} /><Bar dataKey="value" fill="#65d98b" radius={[5, 5, 0, 0]} maxBarSize={32} isAnimationActive={false} /></BarChart></ResponsiveContainer></div> : null}
      </Card>}
      {(visibleCards.has('cost') || visibleCards.has('alerts')) && <div className="right-stack">
        {visibleCards.has('cost') && <Card className="rate-card" title="Current Rate" action={<Clock3 aria-hidden="true" />}>
          {data.current_rate ? <><div className="rate-layout"><div><strong className="rate-period">{data.current_rate.period ?? 'Period unavailable'}</strong><span>{data.current_rate.plan_name}</span><b>{data.current_rate.price_per_kwh === null ? 'Rate unavailable' : `${money(data.current_rate.price_per_kwh)} / kWh`}</b><small>Estimated cost / hour</small><strong>{money(primary.estimated_cost_per_hour)}</strong></div><div className="rate-ring"><span>{new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit' }).format(now)}</span></div></div><a href="/billing">View full rate schedule <ArrowRight aria-hidden="true" /></a></> : <p>No published rate is assigned. Cost remains unavailable until an authorized rate version is selected.</p>}
        </Card>}
        {visibleCards.has('alerts') && <Card title="Alerts & Notifications · all authorized homes" className="compact-alerts">
          {alerts.data?.active_count === 0 && <div className="compact-alert-row"><span className="success-circle">✓</span><div><strong>No critical alerts</strong><small>All alert evidence resolved</small></div></div>}
          {alerts.data?.alerts.slice(0, 3).map((alert) => <div className="compact-alert-row" key={alert.id}><span className={`alert-dot alert-${alert.severity}`} aria-hidden="true" /><div><strong>{alert.type.replaceAll('_', ' ')}</strong><small>Evidence recorded {timeAgo(alert.opened_at)}</small></div><ArrowRight aria-hidden="true" /></div>)}
          <button type="button" className="text-button" onClick={() => window.dispatchEvent(new CustomEvent('pm:open-alerts'))}>View all</button>
        </Card>}
      </div>}
    </section>

    <section className="dashboard-bottom" aria-label="Sensor status and commands">
      <Card title="Sensor Status" className="sensor-status-card" action={<StatusPill state={primary.state} />}>
        <button type="button" className="sensor-name-button" onClick={() => setSelectedDevice(primaryDetail)}>{primary.friendly_name}<ArrowRight aria-hidden="true" /></button>
        <dl className="sensor-quick"><div><dt>Last Heartbeat</dt><dd>{timeAgo(primary.heartbeat_at)}</dd></div><div><dt>microSD Card</dt><dd>{primaryDetail?.storage_status ?? primary.storage_status ?? 'Not available'}</dd></div><div><dt>PZEM Meter</dt><dd>{primaryDetail?.pzem_status ?? primary.measurement?.pzem_status ?? 'Not available'}</dd></div><div><dt>Firmware Version</dt><dd>{primaryDetail?.firmware_version ?? primary.firmware_version ?? 'Not available'}</dd></div><div><dt>Last committed</dt><dd>{timeAgo(primary.last_committed_at)}</dd></div></dl>
      </Card>
      <Card title="Recent Activity / Commands" className="command-card">
        <div className="command-tiles">
          <PermissionGate permission="sensors.command.reboot"><button type="button" onClick={() => setPendingCommand('reboot')}><RotateCcw aria-hidden="true" /><strong>Reboot</strong><small>Safe checkpoint and restart</small><ArrowRight aria-hidden="true" /></button></PermissionGate>
          <PermissionGate permission="sensors.configure"><button type="button" onClick={() => command.mutate({ type: 'sync_now' })}><RefreshCw aria-hidden="true" /><strong>Sync Now</strong><small>Prioritize committed data</small><ArrowRight aria-hidden="true" /></button></PermissionGate>
          <PermissionGate permission="firmware.manage"><a href="/settings?section=firmware"><UploadCloud aria-hidden="true" /><strong>Install OTA</strong><small>Select signed release</small><ArrowRight aria-hidden="true" /></a></PermissionGate>
          <PermissionGate permission="sensors.command.storage_format"><button type="button" className="tile-warning" onClick={() => setPendingCommand('format_storage_prepare')}><HardDrive aria-hidden="true" /><strong>Format SD</strong><small>Prepare / commit only</small><ArrowRight aria-hidden="true" /></button></PermissionGate>
        </div>
      </Card>
    </section>
    </div>
    <SensorDrawer device={selectedDeviceCurrent} open={Boolean(selectedDevice)} onClose={() => setSelectedDevice(undefined)} />
    <ConfirmDialog open={actionableCommand !== null} title={actionableCommand === 'reboot' ? 'Reboot sensor?' : actionableCommand === 'format_storage_commit' ? 'Commit microSD history format?' : 'Prepare to format microSD history?'} description={<p>{actionableCommand === 'reboot' ? 'Measurement pauses briefly while sequence and storage state are checkpointed.' : actionableCommand === 'format_storage_commit' ? formatImpact : 'The prepare step creates a device-bound confirmation. Enrollment, credentials, network configuration and sequence state remain intact.'}</p>} confirmLabel={actionableCommand === 'format_storage_prepare' ? 'Prepare format' : actionableCommand === 'format_storage_commit' ? 'Format history' : 'Queue command'} {...(actionableCommand === 'format_storage_commit' ? { typedPhrase: 'FORMAT STORAGE' } : {})} busy={command.isPending} onCancel={() => { setPendingCommand(null); setFormatEvidence(null); }} onConfirm={() => { if (!actionableCommand) return; command.mutate(actionableCommand === 'format_storage_commit' && formatEvidence ? { type: actionableCommand, prepare: { commandId: formatEvidence.prepareCommandId, confirmationToken: formatEvidence.token }, typedConfirmation: 'FORMAT STORAGE' } : { type: actionableCommand }); }} />
  </div>;
}
