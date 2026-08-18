import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Activity, Database, HardDrive, KeyRound, RefreshCw, RotateCcw, ServerCog, Settings2, ShieldAlert, TestTube2, Trash2, UploadCloud, Wifi } from 'lucide-react';
import { useEffect, useState, type FormEvent } from 'react';
import { api } from '../api';
import type { Command, DeviceDetail } from '../api/schemas';
import { PermissionGate } from '../auth/PermissionGate';
import { formString } from '../lib/form';
import { bytes, numeric, timeAgo } from '../lib/format';
import { HeartbeatAge } from './HeartbeatAge';
import { ConfirmDialog, Dialog, Notice, StatusPill } from './ui';

interface PendingAction {
  type: string;
  title: string;
  phrase?: string;
  warning: string;
  prepare?: { commandId: string; confirmationToken: string };
  payload?: Record<string, unknown>;
}

export function SensorDrawer({ device, open, onClose }: { device: DeviceDetail | undefined; open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [lastCommand, setLastCommand] = useState<Command | null>(null);
  const [awaitingPrepare, setAwaitingPrepare] = useState<{ kind: 'format' | 'reset'; commandId: string; confirmationToken: string } | null>(null);
  const [configureOpen, setConfigureOpen] = useState(false);
  const [scopeDraft, setScopeDraft] = useState('');
  const [revokeOpen, setRevokeOpen] = useState(false);
  const [rotateCredentialOpen, setRotateCredentialOpen] = useState(false);
  const command = useMutation({
    mutationFn: ({ type, payload, prepare, typedConfirmation }: { type: string; payload?: Record<string, unknown>; prepare?: { commandId: string; confirmationToken: string }; typedConfirmation?: string }) => api.command(device?.id ?? '', type, payload, prepare ? { ...prepare, typedConfirmation: typedConfirmation ?? '' } : undefined),
    onSuccess: (result, variables) => {
      setLastCommand(result);
      if (variables.type === 'format_storage_prepare' && result.confirmation_token) {
        setAwaitingPrepare({ kind: 'format', commandId: result.command.id, confirmationToken: result.confirmation_token });
        setPending(null);
      } else if (variables.type === 'data_reset_prepare' && result.confirmation_token) {
        setAwaitingPrepare({ kind: 'reset', commandId: result.command.id, confirmationToken: result.confirmation_token });
        setPending(null);
      } else {
        setPending(null);
        setAwaitingPrepare(null);
      }
      void queryClient.invalidateQueries({ queryKey: ['devices'] });
    },
  });
  const configure = useMutation({ mutationFn: (payload: { friendly_name?: string; location?: string | null; notes?: string | null; display_order?: number; include_in_aggregate?: boolean; show_on_dashboard?: boolean; monitoring_enabled?: boolean; measurement_scope?: string; measurement_scope_confirmation?: string }) => api.updateDevice(device?.id ?? '', payload), onSuccess: () => { setConfigureOpen(false); setScopeDraft(''); void queryClient.invalidateQueries({ queryKey: ['devices'] }); void queryClient.invalidateQueries({ queryKey: ['home'] }); } });
  const revoke = useMutation({ mutationFn: () => api.revokeDevice(device?.id ?? ''), onSuccess: () => { setRevokeOpen(false); onClose(); void queryClient.invalidateQueries({ queryKey: ['devices'] }); void queryClient.invalidateQueries({ queryKey: ['home'] }); } });
  const rotateCredential = useMutation({ mutationFn: () => api.rotateDeviceCredential(device?.id ?? ''), onSuccess: () => { setRotateCredentialOpen(false); void queryClient.invalidateQueries({ queryKey: ['devices'] }); } });
  const cancelCredentialRotation = useMutation({ mutationFn: () => api.cancelDeviceCredentialRotation(device?.id ?? '', device?.credential_rotation?.rotation_id ?? ''), onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['devices'] }); } });

  useEffect(() => {
    const reported = device?.last_command;
    if (!awaitingPrepare || !reported || reported.id !== awaitingPrepare.commandId) return;
    const timer = window.setTimeout(() => {
      if (reported.state === 'succeeded') {
        const evidence = reported.result_evidence ?? {};
        if (awaitingPrepare.kind === 'format' && evidence.ready === true) {
          const acknowledged = typeof evidence.acknowledged_records_lost === 'number' ? evidence.acknowledged_records_lost : 0;
          const unacknowledged = typeof evidence.unacknowledged_records_lost === 'number' ? evidence.unacknowledged_records_lost : 0;
          setPending({ type: 'format_storage_commit', title: 'Commit microSD history format?', phrase: 'FORMAT STORAGE', warning: `The sensor authenticated its prepare result: ${acknowledged} acknowledged and ${unacknowledged} unacknowledged stored records will be removed. Sensor identity, credentials, configuration, sequence floor and acknowledgement are preserved.`, prepare: { commandId: awaitingPrepare.commandId, confirmationToken: awaitingPrepare.confirmationToken } });
          setAwaitingPrepare(null);
        } else if (awaitingPrepare.kind === 'reset' && evidence.ready === true) {
          const generation = typeof evidence.reset_generation === 'number' ? evidence.reset_generation : 'unavailable';
          const floor = typeof evidence.sequence_floor === 'number' ? evidence.sequence_floor : 'unavailable';
          setPending({ type: 'data_reset_commit', title: 'Commit data-only reset?', phrase: 'CLEAR READINGS', warning: `The sensor authenticated reset generation ${generation} at sequence floor ${floor}. Selected server readings, derived intervals, costs and sensor history will be hidden from current History while immutable audit evidence is retained. Enrollment and configuration are preserved.`, prepare: { commandId: awaitingPrepare.commandId, confirmationToken: awaitingPrepare.confirmationToken } });
          setAwaitingPrepare(null);
        }
      } else if (['failed', 'expired', 'cancelled', 'superseded'].includes(reported.state)) {
        setAwaitingPrepare(null);
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [awaitingPrepare, device?.last_command]);

  if (!device) return null;
  const request = (action: PendingAction) => setPending(action);
  function submitConfiguration(event: FormEvent<HTMLFormElement>) { event.preventDefault(); const form = new FormData(event.currentTarget); const scope = formString(form, 'measurementScope'); const confirmation = formString(form, 'measurementScopeConfirmation'); const location = formString(form, 'location'); const notes = formString(form, 'notes'); configure.mutate({ friendly_name: formString(form, 'friendlyName'), location: location || null, notes: notes || null, display_order: Number(formString(form, 'displayOrder')), include_in_aggregate: form.has('includeInAggregate'), show_on_dashboard: form.has('showOnDashboard'), monitoring_enabled: form.has('monitoringEnabled'), ...(scope ? { measurement_scope: scope } : {}), ...(confirmation ? { measurement_scope_confirmation: confirmation } : {}) }); }
  return <>
    <Dialog open={open} title={device.friendly_name} description={`Device ${device.device_fingerprint}`} onClose={onClose} wide>
      <div className="sensor-drawer-heading"><StatusPill state={device.heartbeat_at ? 'online' : 'offline'} /><span>Heartbeat <HeartbeatAge timestamp={device.heartbeat_at} /></span></div>
      {lastCommand && <Notice kind="success">Command {lastCommand.command.id} was queued. Success appears only after authenticated device completion evidence.</Notice>}
      {awaitingPrepare && <Notice kind="info">Prepare command {awaitingPrepare.commandId} is queued. Commit stays unavailable until the sensor returns authenticated readiness and impact evidence.</Notice>}
      {command.isError && <Notice kind="warning">{command.error instanceof Error ? command.error.message : 'The command could not be queued.'}</Notice>}
      {rotateCredential.isError && <Notice kind="warning">{rotateCredential.error instanceof Error ? rotateCredential.error.message : 'Credential rotation could not be started.'}</Notice>}
      {cancelCredentialRotation.isError && <Notice kind="warning">{cancelCredentialRotation.error instanceof Error ? cancelCredentialRotation.error.message : 'Credential rotation could not be cancelled.'}</Notice>}
      {device.credential_rotation && <Notice kind="info">Credential rotation {device.credential_rotation.state}: candidate {device.credential_rotation.credential_fingerprint.slice(0, 16)}… expires {timeAgo(device.credential_rotation.overlap_expires_at)}. No device secret is returned to this browser.</Notice>}
      <div className="detail-grid">
        <section><h3><ServerCog aria-hidden="true" /> Device</h3><dl><div><dt>Location</dt><dd>{device.location ?? 'Not set'}</dd></div><div><dt>Firmware</dt><dd>{device.firmware_version ?? 'Not available'}</dd></div><div><dt>Protocol</dt><dd>{device.protocol}</dd></div><div><dt>Meter variant</dt><dd>{device.pzem_variant}</dd></div><div><dt>Credential</dt><dd>{device.credential_fingerprint ? `${device.credential_fingerprint.slice(0, 16)}… · key ${device.credential_key_version ?? 'unknown'}` : 'Not available'}</dd></div><div><dt>Last reboot</dt><dd>{device.last_reboot_reason ?? 'Not available'}</dd></div></dl></section>
        <section><h3><Wifi aria-hidden="true" /> Network</h3><dl><div><dt>Wi-Fi</dt><dd>{device.heartbeat_at ? 'connected' : 'unavailable'}</dd></div><div><dt>RSSI</dt><dd>{numeric(device.wifi_rssi, 'dBm', 0)}</dd></div><div><dt>IP address</dt><dd>{device.ip_address ?? 'Not available'}</dd></div></dl></section>
        <section><h3><Activity aria-hidden="true" /> Measurement</h3><dl><div><dt>PZEM state</dt><dd>{device.pzem_status}</dd></div><div><dt>CT rating</dt><dd>{numeric(device.ct_rating_a === null ? null : Number(device.ct_rating_a), 'A', 0)}</dd></div><div><dt>Backlog</dt><dd>{numeric(device.backlog, 'intervals', 0)}</dd></div></dl></section>
        <section><h3><HardDrive aria-hidden="true" /> microSD</h3><dl><div><dt>State</dt><dd>{device.storage_status}</dd></div><div><dt>Capacity / free</dt><dd>{device.storage_bytes_total === null || device.storage_bytes_free === null ? 'Not reported' : `${bytes(device.storage_bytes_total)} total · ${bytes(device.storage_bytes_free)} free`}</dd></div><div><dt>Stored sequences</dt><dd>{device.oldest_sequence ?? '—'}–{device.newest_sequence ?? '—'}</dd></div><div><dt>Server acknowledged</dt><dd>{device.acknowledgement}</dd></div></dl></section>
        <section><h3><Database aria-hidden="true" /> Memory</h3><dl><div><dt>Free heap</dt><dd>{bytes(device.free_internal_heap)}</dd></div><div><dt>Largest block</dt><dd>{bytes(device.largest_internal_block)}</dd></div><div><dt>Task stack watermarks</dt><dd>Not reported</dd></div></dl></section>
        <section><h3><RefreshCw aria-hidden="true" /> Command & OTA</h3><dl><div><dt>Last command</dt><dd>{device.last_command ? `${device.last_command.type} · ${device.last_command.state}` : 'None'}</dd></div><div><dt>Progress</dt><dd>{device.last_command ? `${device.last_command.progress_percent}%` : 'Not available'}</dd></div><div><dt>Result</dt><dd>{device.last_command?.result_code ?? 'Not reported'}</dd></div><div><dt>OTA release</dt><dd>Not reported</dd></div></dl></section>
      </div>
      <h3 className="section-label">Sensor controls</h3>
      <div className="control-grid">
        <PermissionGate permission="sensors.configure"><button type="button" className="control-button" onClick={() => command.mutate({ type: 'diagnostics_snapshot' })}><Activity aria-hidden="true" /><span>Run diagnostics<small>Collect redacted evidence</small></span></button><button type="button" className="control-button" onClick={() => command.mutate({ type: 'sync_now' })}><RefreshCw aria-hidden="true" /><span>Sync now<small>Prioritize saved readings</small></span></button><button type="button" className="control-button" onClick={() => command.mutate({ type: 'network_self_test' })}><Wifi aria-hidden="true" /><span>Network self-test<small>DNS, TLS and server checks</small></span></button></PermissionGate>
        <PermissionGate permission="sensors.configure"><button type="button" className="control-button" disabled={Boolean(device.credential_rotation)} onClick={() => setRotateCredentialOpen(true)}><KeyRound aria-hidden="true" /><span>Rotate credential<small>Server-generated two-key handoff</small></span></button>{device.credential_rotation && <button type="button" className="control-button control-warning" onClick={() => cancelCredentialRotation.mutate()} disabled={cancelCredentialRotation.isPending}><KeyRound aria-hidden="true" /><span>Cancel rotation<small>Zeroize the pending candidate</small></span></button>}</PermissionGate>
        <PermissionGate permission="sensors.command.storage_test"><button type="button" className="control-button" onClick={() => command.mutate({ type: 'meter_self_test' })}><TestTube2 aria-hidden="true" /><span>Test PZEM<small>Does not fabricate readings</small></span></button><button type="button" className="control-button" onClick={() => command.mutate({ type: 'storage_self_test' })}><HardDrive aria-hidden="true" /><span>Test microSD<small>Bounded storage self-test</small></span></button></PermissionGate>
        <PermissionGate permission="sensors.command.reboot"><button type="button" className="control-button" onClick={() => request({ type: 'reboot', title: 'Reboot sensor?', warning: 'Measurement pauses briefly while storage and sequence state are safely checkpointed.' })}><RotateCcw aria-hidden="true" /><span>Reboot<small>Safe checkpoint and restart</small></span></button></PermissionGate>
        <PermissionGate permission="sensors.command.sleep"><button type="button" className="control-button control-warning" onClick={() => request({ type: 'maintenance_sleep', title: 'Start maintenance sleep?', warning: 'Monitoring and reporting stop during the selected short sleep. This does not disconnect mains power.', payload: { seconds: 300 } })}><ShieldAlert aria-hidden="true" /><span>Maintenance sleep<small>5 minutes; physical power unaffected</small></span></button></PermissionGate>
        <PermissionGate permission="firmware.manage"><a className="control-button" href="/settings?section=firmware"><UploadCloud aria-hidden="true" /><span>Install OTA<small>Select a signed server release</small></span></a></PermissionGate>
        <PermissionGate permission="sensors.command.storage_format"><button type="button" className="control-button control-danger" onClick={() => request({ type: 'format_storage_prepare', title: 'Prepare to format microSD history?', warning: 'The prepare step creates a device-bound confirmation token. Enrollment, network settings, identity, sequence floor, acknowledgement and OTA state are preserved.' })}><HardDrive aria-hidden="true" /><span>Format microSD history<small>Two-step prepare and commit</small></span></button></PermissionGate>
        <PermissionGate permission="sensors.command.data_reset"><button type="button" className="control-button control-danger" onClick={() => request({ type: 'data_reset_prepare', title: 'Prepare to clear sensor-derived readings?', warning: 'A separate typed commit preserves enrollment and prevents pre-reset records from repopulating History.' })}><Database aria-hidden="true" /><span>Clear readings<small>Preserves enrollment</small></span></button></PermissionGate>
      </div>
      <PermissionGate permission="sensors.configure"><div className="sensor-admin-actions"><button type="button" className="button button-secondary" onClick={() => { setScopeDraft(''); setConfigureOpen(true); }}><Settings2 aria-hidden="true" /> Configure sensor</button><button type="button" className="button button-danger" onClick={() => setRevokeOpen(true)}><Trash2 aria-hidden="true" /> Revoke sensor</button></div></PermissionGate>
    </Dialog>
    <ConfirmDialog open={pending !== null} title={pending?.title ?? ''} description={<p>{pending?.warning}</p>} confirmLabel="Queue command" {...(pending?.phrase ? { typedPhrase: pending.phrase } : {})} busy={command.isPending} onCancel={() => { if (pending?.type === 'data_reset_commit' && pending.prepare) { command.mutate({ type: 'data_reset_cancel', payload: { prepare_command_id: pending.prepare.commandId } }); } else { setPending(null); } }} onConfirm={() => { if (pending) command.mutate({ type: pending.type, ...(pending.payload ? { payload: pending.payload } : {}), ...(pending.prepare ? { prepare: pending.prepare } : {}), ...(pending.phrase ? { typedConfirmation: pending.phrase } : {}) }); }} />
    <Dialog open={configureOpen} title={`Configure ${device.friendly_name}`} description="Identity, display, monitoring and measurement settings are stored centrally and audited." onClose={() => setConfigureOpen(false)}>
      <form className="settings-form" onSubmit={submitConfiguration}>
        <div className="field"><label htmlFor="sensor-friendly-name">Friendly name</label><input id="sensor-friendly-name" name="friendlyName" defaultValue={device.friendly_name} required maxLength={120} /></div>
        <div className="field"><label htmlFor="sensor-location">Location</label><input id="sensor-location" name="location" defaultValue={device.location ?? ''} maxLength={120} placeholder="Main electrical panel" /></div>
        <div className="field"><label htmlFor="sensor-notes">Notes</label><textarea id="sensor-notes" name="notes" defaultValue={device.notes ?? ''} maxLength={500} rows={3} /></div>
        <div className="field"><label htmlFor="sensor-display-order">Display order</label><input id="sensor-display-order" name="displayOrder" type="number" min="0" max="10000" step="1" defaultValue={device.display_order} required /></div>
        <div className="appearance-options">
          <label><input type="checkbox" name="showOnDashboard" defaultChecked={device.show_on_dashboard} /><span><strong>Show on dashboard</strong><small>Hide this sensor card without deleting readings.</small></span></label>
          <label><input type="checkbox" name="includeInAggregate" defaultChecked={device.include_in_aggregate} /><span><strong>Eligible for service branches</strong><small>Sensors must still be confirmed not to measure the same electricity.</small></span></label>
          <label><input type="checkbox" name="monitoringEnabled" defaultChecked={device.monitoring_enabled} /><span><strong>Operational monitoring</strong><small>Disabling alerts and availability status never stops secure sensor uploads.</small></span></label>
        </div>
        <div className="field"><label htmlFor="sensor-measurement-scope">Measurement source</label><select id="sensor-measurement-scope" name="measurementScope" value={scopeDraft} onChange={(event) => setScopeDraft(event.target.value)}><option value="">Leave current server value unchanged</option><option value="energy_only">Energy charges only</option><option value="allocated_account">Allocated account</option><option value="full_account">Full account</option></select><small>Changes require explicit verification. The current selection is {device.measurement_scope === 'energy_only' ? 'energy charges only' : device.measurement_scope.replaceAll('_', ' ')}.</small></div>
        {scopeDraft === 'full_account' && <div className="field"><label htmlFor="sensor-full-scope-confirmation">Type I VERIFIED THIS METER COVERS THE FULL ACCOUNT</label><input id="sensor-full-scope-confirmation" name="measurementScopeConfirmation" required pattern="I VERIFIED THIS METER COVERS THE FULL ACCOUNT" autoComplete="off" /></div>}
        {scopeDraft === 'allocated_account' && <div className="field"><label htmlFor="sensor-allocated-scope-confirmation">Type I VERIFIED THIS ALLOCATION SCOPE</label><input id="sensor-allocated-scope-confirmation" name="measurementScopeConfirmation" required pattern="I VERIFIED THIS ALLOCATION SCOPE" autoComplete="off" /></div>}
        {configure.isError && <Notice kind="warning">{configure.error instanceof Error ? configure.error.message : 'Sensor configuration failed.'}</Notice>}
        <div className="dialog-actions"><button type="button" className="button button-secondary" onClick={() => setConfigureOpen(false)}>Cancel</button><button type="submit" className="button button-primary" disabled={configure.isPending}>{configure.isPending ? 'Saving…' : 'Save sensor'}</button></div>
      </form>
    </Dialog>
    <ConfirmDialog open={revokeOpen} title={`Revoke ${device.friendly_name}?`} description={<p>Revocation stops future authenticated communication and invalidates active device credentials. Existing immutable readings and History remain preserved.</p>} confirmLabel="Revoke sensor" typedPhrase="REVOKE SENSOR" busy={revoke.isPending} onCancel={() => setRevokeOpen(false)} onConfirm={() => revoke.mutate()} tone="danger" />
    <ConfirmDialog open={rotateCredentialOpen} title={`Rotate ${device.friendly_name} credentials?`} description={<p>The server generates the candidate and sends it only inside an authenticated sensor command. This browser receives only a fingerprint and progress state. The old key remains valid for at most ten minutes and is revoked only after a completion signed by the new key.</p>} confirmLabel="Start rotation" typedPhrase="ROTATE SENSOR CREDENTIALS" busy={rotateCredential.isPending} onCancel={() => setRotateCredentialOpen(false)} onConfirm={() => rotateCredential.mutate()} tone="warning" />
  </>;
}
