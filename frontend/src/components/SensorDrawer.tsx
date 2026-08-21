import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Activity, Cpu, RotateCcw, Server, Settings2, Trash2, UploadCloud, Wifi } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { api } from '../api';
import type { Command, DeviceDetail } from '../api/schemas';
import { PermissionGate } from '../auth/PermissionGate';
import { formString } from '../lib/form';
import { bytes, numeric } from '../lib/format';
import { HeartbeatAge } from './HeartbeatAge';
import { ConfirmDialog, Dialog, Notice, StatusPill } from './ui';

function deliveryLabel(device: DeviceDetail) {
  const status = device.server_delivery_status ?? device.synchronization?.server_delivery_status;
  return status ? status.replaceAll('_', ' ') : 'Not reported by the server';
}

export function SensorDrawer({ device, open, onClose }: { device: DeviceDetail | undefined; open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [rebootOpen, setRebootOpen] = useState(false);
  const [lastCommand, setLastCommand] = useState<Command | null>(null);
  const [configureOpen, setConfigureOpen] = useState(false);
  const [scopeDraft, setScopeDraft] = useState('');
  const [revokeOpen, setRevokeOpen] = useState(false);
  const command = useMutation({ mutationFn: () => api.command(device?.id ?? '', 'reboot'), onSuccess: (result) => { setLastCommand(result); setRebootOpen(false); void queryClient.invalidateQueries({ queryKey: ['devices'] }); } });
  const configure = useMutation({ mutationFn: (payload: { friendly_name?: string; location?: string | null; notes?: string | null; display_order?: number; include_in_aggregate?: boolean; show_on_dashboard?: boolean; monitoring_enabled?: boolean; measurement_scope?: string; measurement_scope_confirmation?: string }) => api.updateDevice(device?.id ?? '', payload), onSuccess: () => { setConfigureOpen(false); setScopeDraft(''); void queryClient.invalidateQueries({ queryKey: ['devices'] }); void queryClient.invalidateQueries({ queryKey: ['home'] }); } });
  const revoke = useMutation({ mutationFn: () => api.revokeDevice(device?.id ?? ''), onSuccess: () => { setRevokeOpen(false); onClose(); void queryClient.invalidateQueries({ queryKey: ['devices'] }); void queryClient.invalidateQueries({ queryKey: ['home'] }); } });
  if (!device) return null;
  const lastReceivedAt = device.last_server_received_at ?? device.synchronization?.last_server_received_at;
  const lastSampledAt = device.last_sensor_sampled_at ?? device.synchronization?.last_sensor_sampled_at;
  const sensorTimeTrusted = device.sensor_time_trusted ?? device.synchronization?.sensor_time_trusted;

  function submitConfiguration(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const scope = formString(form, 'measurementScope');
    const confirmation = formString(form, 'measurementScopeConfirmation');
    const location = formString(form, 'location');
    const notes = formString(form, 'notes');
    configure.mutate({ friendly_name: formString(form, 'friendlyName'), location: location || null, notes: notes || null, display_order: Number(formString(form, 'displayOrder')), include_in_aggregate: form.has('includeInAggregate'), show_on_dashboard: form.has('showOnDashboard'), monitoring_enabled: form.has('monitoringEnabled'), ...(scope ? { measurement_scope: scope } : {}), ...(confirmation ? { measurement_scope_confirmation: confirmation } : {}) });
  }

  return <>
    <Dialog open={open} title={device.friendly_name} description={`Sensor ${device.device_fingerprint}`} onClose={onClose} wide>
      <div className="sensor-drawer-heading"><StatusPill state={device.heartbeat_at ? 'online' : 'offline'} /><span>Last contact <HeartbeatAge timestamp={device.heartbeat_at} /></span></div>
      {lastCommand && <Notice kind="success">The restart request was sent. The sensor will confirm when it reconnects.</Notice>}
      {command.isError && <Notice kind="warning">{command.error instanceof Error ? command.error.message : 'The restart request could not be sent.'}</Notice>}
      <div className="detail-grid">
        <section><h3><Cpu aria-hidden="true" /> Sensor</h3><dl><div><dt>Location</dt><dd>{device.location ?? 'Not set'}</dd></div><div><dt>Firmware</dt><dd>{device.firmware_version ?? 'Not available'}</dd></div><div><dt>Meter</dt><dd>{device.pzem_variant}</dd></div><div><dt>Last restart</dt><dd>{device.last_reboot_reason ?? 'Not available'}</dd></div></dl></section>
        <section><h3><Wifi aria-hidden="true" /> Connection</h3><dl><div><dt>Wi-Fi</dt><dd>{device.heartbeat_at ? 'Connected' : 'Not connected'}</dd></div><div><dt>Signal</dt><dd>{numeric(device.wifi_rssi, 'dBm', 0)}</dd></div><div><dt>IP address</dt><dd>{device.ip_address ?? 'Not available'}</dd></div></dl></section>
        <section><h3><Activity aria-hidden="true" /> Measurement</h3><dl><div><dt>Meter state</dt><dd>{device.pzem_status}</dd></div><div><dt>CT rating</dt><dd>{numeric(device.ct_rating_a === null ? null : Number(device.ct_rating_a), 'A', 0)}</dd></div><div><dt>Total energy</dt><dd>{numeric(device.cumulative_energy_kwh ?? null, 'kWh', 3)}</dd></div><div><dt>Sample time trusted</dt><dd>{sensorTimeTrusted === undefined || sensorTimeTrusted === null ? 'Not reported' : sensorTimeTrusted ? 'Yes' : 'No'}</dd></div></dl></section>
        <section><h3><Server aria-hidden="true" /> Server delivery</h3><dl><div><dt>Status</dt><dd>{deliveryLabel(device)}</dd></div><div><dt>Last received</dt><dd><HeartbeatAge timestamp={lastReceivedAt ?? null} /></dd></div><div><dt>Last measured</dt><dd><HeartbeatAge timestamp={lastSampledAt ?? null} /></dd></div></dl></section>
        <section><h3><Activity aria-hidden="true" /> Device memory</h3><dl><div><dt>Available memory</dt><dd>{bytes(device.free_internal_heap)}</dd></div><div><dt>Largest available block</dt><dd>{bytes(device.largest_internal_block)}</dd></div></dl></section>
        <section><h3><UploadCloud aria-hidden="true" /> Firmware update</h3><dl><div><dt>Last action</dt><dd>{device.last_command ? `${device.last_command.type.replaceAll('_', ' ')} · ${device.last_command.state}` : 'None'}</dd></div><div><dt>Progress</dt><dd>{device.last_command ? `${device.last_command.progress_percent}%` : 'Not available'}</dd></div><div><dt>Result</dt><dd>{device.last_command?.result_code ?? 'Not reported'}</dd></div></dl></section>
      </div>
      <h3 className="section-label">Sensor controls</h3>
      <div className="control-grid"><PermissionGate permission="sensors.command.reboot"><button type="button" className="control-button" onClick={() => setRebootOpen(true)}><RotateCcw aria-hidden="true" /><span>Reboot<small>Restart this sensor</small></span></button></PermissionGate><PermissionGate permission="firmware.manage"><a className="control-button" href="/settings?section=firmware"><UploadCloud aria-hidden="true" /><span>Install OTA<small>Select a signed firmware release</small></span></a></PermissionGate></div>
      <PermissionGate permission="sensors.configure"><div className="sensor-admin-actions"><button type="button" className="button button-secondary" onClick={() => { setScopeDraft(''); setConfigureOpen(true); }}><Settings2 aria-hidden="true" /> Configure sensor</button><button type="button" className="button button-danger" onClick={() => setRevokeOpen(true)}><Trash2 aria-hidden="true" /> Revoke sensor</button></div></PermissionGate>
    </Dialog>
    <ConfirmDialog open={rebootOpen} title={`Reboot ${device.friendly_name}?`} description={<p>Measurements pause briefly while the sensor restarts and reconnects.</p>} confirmLabel="Reboot sensor" busy={command.isPending} onCancel={() => setRebootOpen(false)} onConfirm={() => command.mutate()} />
    <Dialog open={configureOpen} title={`Configure ${device.friendly_name}`} description="Identity, display and measurement settings are stored centrally and audited." onClose={() => setConfigureOpen(false)}>
      <form className="settings-form" onSubmit={submitConfiguration}>
        <div className="field"><label htmlFor="sensor-friendly-name">Friendly name</label><input id="sensor-friendly-name" name="friendlyName" defaultValue={device.friendly_name} required maxLength={120} /></div>
        <div className="field"><label htmlFor="sensor-location">Location</label><input id="sensor-location" name="location" defaultValue={device.location ?? ''} maxLength={120} placeholder="Main electrical panel" /></div>
        <div className="field"><label htmlFor="sensor-notes">Notes</label><textarea id="sensor-notes" name="notes" defaultValue={device.notes ?? ''} maxLength={500} rows={3} /></div>
        <div className="field"><label htmlFor="sensor-display-order">Display order</label><input id="sensor-display-order" name="displayOrder" type="number" min="0" max="10000" step="1" defaultValue={device.display_order} required /></div>
        <div className="appearance-options"><label><input type="checkbox" name="showOnDashboard" defaultChecked={device.show_on_dashboard} /><span><strong>Show on dashboard</strong><small>Hide this sensor without deleting readings.</small></span></label><label><input type="checkbox" name="includeInAggregate" defaultChecked={device.include_in_aggregate} /><span><strong>Eligible for service branches</strong><small>Sensors must be confirmed not to measure the same electricity.</small></span></label><label><input type="checkbox" name="monitoringEnabled" defaultChecked={device.monitoring_enabled} /><span><strong>Operational monitoring</strong><small>Disabling alerts does not stop secure sensor uploads.</small></span></label></div>
        <div className="field"><label htmlFor="sensor-measurement-scope">Measurement source</label><select id="sensor-measurement-scope" name="measurementScope" value={scopeDraft} onChange={(event) => setScopeDraft(event.target.value)}><option value="">Leave unchanged</option><option value="energy_only">Energy charges only</option><option value="allocated_account">Allocated account</option><option value="full_account">Full account</option></select></div>
        {scopeDraft === 'full_account' && <div className="field"><label htmlFor="sensor-full-scope-confirmation">Type I VERIFIED THIS METER COVERS THE FULL ACCOUNT</label><input id="sensor-full-scope-confirmation" name="measurementScopeConfirmation" required pattern="I VERIFIED THIS METER COVERS THE FULL ACCOUNT" autoComplete="off" /></div>}
        {scopeDraft === 'allocated_account' && <div className="field"><label htmlFor="sensor-allocated-scope-confirmation">Type I VERIFIED THIS ALLOCATION SCOPE</label><input id="sensor-allocated-scope-confirmation" name="measurementScopeConfirmation" required pattern="I VERIFIED THIS ALLOCATION SCOPE" autoComplete="off" /></div>}
        {configure.isError && <Notice kind="warning">{configure.error instanceof Error ? configure.error.message : 'Sensor configuration failed.'}</Notice>}
        <div className="dialog-actions"><button type="button" className="button button-secondary" onClick={() => setConfigureOpen(false)}>Cancel</button><button type="submit" className="button button-primary" disabled={configure.isPending}>{configure.isPending ? 'Saving…' : 'Save sensor'}</button></div>
      </form>
    </Dialog>
    <ConfirmDialog open={revokeOpen} title={`Revoke ${device.friendly_name}?`} description={<p>Revocation stops future authenticated communication. Existing readings remain preserved.</p>} confirmLabel="Revoke sensor" typedPhrase="REVOKE SENSOR" busy={revoke.isPending} onCancel={() => setRevokeOpen(false)} onConfirm={() => revoke.mutate()} tone="danger" />
  </>;
}
