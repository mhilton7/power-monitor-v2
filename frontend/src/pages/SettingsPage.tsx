import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, ArchiveRestore, ChevronRight, Cpu, Download, FileClock, HardDrive, Home, KeyRound, Palette, RefreshCw, RotateCcw, ServerCog, Shield, Trash2, UploadCloud, UserPlus, Users, Wifi } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { api } from '../api';
import { isForbidden } from '../api/client';
import type { Circuit, DeviceDetail, FirmwareDeploymentBatch, FirmwareRelease, FirmwareReleaseList, User as UserType } from '../api/schemas';
import { PermissionGate } from '../auth/PermissionGate';
import { useSession } from '../auth/SessionContext';
import { SensorDrawer } from '../components/SensorDrawer';
import { Card, ConfirmDialog, Dialog, EmptyState, ErrorState, Loading, Notice, StatusPill } from '../components/ui';
import { formString } from '../lib/form';
import { bytes, dateTime, download } from '../lib/format';
import { HeartbeatAge } from '../components/HeartbeatAge';
import { firmwareUpgradeAvailable, prepareFirmwareUpload, type FirmwareUploadFields, type PreparedFirmwareUpload } from '../lib/firmwareUpload';
import { useHomeScope } from '../home/useHomeScope';
import { BillingPage } from './BillingPage';

type SectionId = 'home' | 'sensors' | 'users' | 'rates' | 'firmware' | 'backups' | 'appearance' | 'privacy' | 'health' | 'logs';
interface SettingsSection { id: SectionId; label: string; icon: ReactNode; permission: string; }
const sections: SettingsSection[] = [
  { id: 'home', label: 'Home', icon: <Home aria-hidden="true" />, permission: 'billing.view' },
  { id: 'sensors', label: 'Sensors', icon: <Wifi aria-hidden="true" />, permission: 'sensors.view' },
  { id: 'users', label: 'Profile & users', icon: <Users aria-hidden="true" />, permission: 'dashboard.view' },
  { id: 'rates', label: 'Rates & data sources', icon: <FileClock aria-hidden="true" />, permission: 'rates.view' },
  { id: 'firmware', label: 'Firmware', icon: <Cpu aria-hidden="true" />, permission: 'firmware.view' },
  { id: 'backups', label: 'Backups & restore', icon: <ArchiveRestore aria-hidden="true" />, permission: 'backups.view' },
  { id: 'appearance', label: 'Appearance', icon: <Palette aria-hidden="true" />, permission: 'dashboard.view' },
  { id: 'privacy', label: 'Data & privacy', icon: <Shield aria-hidden="true" />, permission: 'dashboard.view' },
  { id: 'health', label: 'Diagnostics', icon: <Activity aria-hidden="true" />, permission: 'system.view' },
  { id: 'logs', label: 'Logs & diagnostics', icon: <ServerCog aria-hidden="true" />, permission: 'logs.view' },
];

function timeAgo(value: string | null | undefined) {
  return <HeartbeatAge timestamp={value} />;
}

export function SettingsPage() {
  const { can } = useSession();
  const visible = useMemo(() => sections.filter((section) => can(section.permission)), [can]);
  const [active, setActive] = useState<SectionId>(() => {
    const requested = new URLSearchParams(window.location.search).get('section');
    return sections.some((section) => section.id === requested) ? requested as SectionId : visible[0]?.id ?? 'appearance';
  });
  const homeScope = useHomeScope();
  const { selectedHomeId, selectedHome } = homeScope;
  const devices = useQuery({ queryKey: ['devices', selectedHomeId], queryFn: () => api.devices(selectedHomeId), enabled: can('sensors.view') && Boolean(selectedHomeId), refetchInterval: 30_000 });
  const users = useQuery({ queryKey: ['users'], queryFn: api.users, enabled: can('users.view') });
  const roles = useQuery({ queryKey: ['roles'], queryFn: api.roles, enabled: can('users.view') });
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, enabled: can('system.view'), refetchInterval: 60_000 });
  const backups = useQuery({ queryKey: ['backups'], queryFn: api.backups, enabled: can('backups.view'), refetchInterval: 60_000 });
  const firmware = useQuery({ queryKey: ['firmware-releases'], queryFn: () => api.firmwareReleases(), enabled: can('firmware.view'), refetchInterval: 5_000 });
  if (visible.length === 0) return <div className="page"><h1 className="sr-only">Settings</h1><EmptyState title="No settings available" detail="Your role does not include access to any settings area." /></div>;
  const selected = visible.some((section) => section.id === active) ? active : visible[0]!.id;
  return <div className="page settings-page">
    <header className="page-heading"><div><p className="eyebrow">Permission-scoped configuration</p><h1>Settings</h1><p>Manage your home, sensors and operations without exposing device credentials.</p></div></header>
    <div className="settings-layout"><nav className="settings-nav" aria-label="Settings sections">{visible.map((section) => <button key={section.id} type="button" className={selected === section.id ? 'active' : ''} aria-label={section.label} aria-current={selected === section.id ? 'page' : undefined} onClick={() => setActive(section.id)}>{section.icon}<span>{section.label}</span><ChevronRight aria-hidden="true" /></button>)}</nav><div className="settings-content">
      {selected === 'home' && <HomeSettings />}
      {selected === 'sensors' && <SensorSettings homeScopes={selectedHome ? [selectedHome] : []} devices={devices.data?.devices ?? []} loading={homeScope.isLoading || devices.isLoading} error={homeScope.error ?? devices.error} />}
      {selected === 'users' && <UserSettings users={users.data?.users ?? []} roles={roles.data?.roles ?? []} loading={users.isLoading || roles.isLoading} error={users.error ?? roles.error} />}
      {selected === 'rates' && <><RateSettings homeId={selectedHomeId} /><SensorSettings mode="billing" homeScopes={selectedHome ? [selectedHome] : []} devices={devices.data?.devices ?? []} loading={homeScope.isLoading || devices.isLoading} error={homeScope.error ?? devices.error} /></>}
      {selected === 'firmware' && <FirmwareSettings devices={devices.data?.devices ?? []} releases={firmware.data?.releases ?? []} reconciliation={firmware.data?.reconciliation} loading={firmware.isLoading} error={firmware.error} />}
      {selected === 'backups' && <BackupSettings backup={backups.data} loading={backups.isLoading} error={backups.error} />}
      {selected === 'appearance' && <AppearanceSettings />}
      {selected === 'privacy' && <DataPrivacySettings />}
      {selected === 'health' && <HealthSettings homeId={selectedHomeId} health={health.data} devices={devices.data?.devices ?? []} loading={health.isLoading} error={health.error} />}
      {selected === 'logs' && <LogsSettings />}
    </div></div>
  </div>;
}

function HomeSettings() {
  const { can } = useSession();
  const homeScope = useHomeScope();
  const { selectedHomeId } = homeScope;
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ['home-utility', selectedHomeId], queryFn: () => api.homeUtility(selectedHomeId), enabled: Boolean(selectedHomeId) });
  const update = useMutation({ mutationFn: (payload: Record<string, unknown>) => api.updateHomeUtility(selectedHomeId, payload), onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['home-utility'] }); void queryClient.invalidateQueries({ queryKey: ['home'] }); void queryClient.invalidateQueries({ queryKey: ['home-scopes'] }); homeScope.refetch(); } });
  if (homeScope.isLoading) return <Card title="Home"><Loading label="Loading authorized homes" /></Card>;
  if (homeScope.isError) return <ErrorState error={homeScope.error} retry={homeScope.refetch} />;
  if (!selectedHomeId) return <EmptyState title={homeScope.homeScopes.length === 0 ? 'No authorized home' : 'Choose an active home'} detail={homeScope.homeScopes.length === 0 ? 'Your account has no authorized home scope. Home settings remain unavailable.' : 'Select a home from the Active home control before loading or changing settings.'} />;
  if (query.isLoading) return <Card title="Home"><Loading /></Card>;
  if (query.isError) return <ErrorState error={query.error} retry={() => void query.refetch()} />;
  if (!query.data) return <EmptyState title="Home settings unavailable" detail="The server returned no home or utility account." />;
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    update.mutate({ home_name: formString(form, 'homeName') });
  }
  return <Card title="Home" eyebrow="Identity"><form key={query.data.home.id} className="settings-form" onSubmit={submit}>
    <div className="field"><label htmlFor="home-setting-name">Home name</label><input id="home-setting-name" name="homeName" defaultValue={query.data.home.name} required maxLength={120} disabled={!can('system.manage')} /></div>
    <Notice>Billing schedule, timezone, baseline, generation service, estimate policy, and billing source are managed only in Rates &amp; data sources.</Notice>
    {update.isError && <Notice kind="warning">{update.error instanceof Error ? update.error.message : 'Settings could not be saved.'}</Notice>}
    {update.isSuccess && <Notice kind="success">The home name was saved by the server.</Notice>}
    {can('system.manage') && <button type="submit" className="button button-primary" disabled={update.isPending}>{update.isPending ? 'Saving…' : 'Save home name'}</button>}
  </form></Card>;
}

function SensorSettings({ homeScopes, devices, loading, error, mode = 'sensors' }: { homeScopes: Array<{ id: string; name: string }>; devices: DeviceDetail[]; loading: boolean; error: unknown; mode?: 'sensors' | 'billing' }) {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<DeviceDetail>();
  const [enrollOpen, setEnrollOpen] = useState(false);
  const [enrollmentHomeId, setEnrollmentHomeId] = useState('');
  const [branchOpen, setBranchOpen] = useState(false);
  const [editingBranch, setEditingBranch] = useState<Circuit>();
  const [branchDevices, setBranchDevices] = useState<string[]>([]);
  const [branchPurpose, setBranchPurpose] = useState<'electrical_section' | 'whole_home_total'>('electrical_section');
  const [branchBillingSource, setBranchBillingSource] = useState(false);
  const [deleteBranch, setDeleteBranch] = useState<Circuit>();
  const activeHomeId = homeScopes[0]?.id ?? '';
  const circuits = useQuery({ queryKey: ['circuits', activeHomeId], queryFn: () => api.circuits(activeHomeId), enabled: Boolean(activeHomeId) });
  const onlyHomeId = homeScopes.length === 1 ? homeScopes[0]!.id : undefined;
  const scopedEnrollmentHomeId = onlyHomeId ?? (homeScopes.some((home) => home.id === enrollmentHomeId) ? enrollmentHomeId : undefined);
  const scopedDevices = activeHomeId ? devices.filter((device) => device.home_id === activeHomeId) : [];
  const enrollment = useMutation({ mutationFn: (payload: { friendlyName: string; ctRating: string; homeId: string }) => api.createEnrollmentToken({ home_id: payload.homeId, friendly_name: payload.friendlyName, ct_rating_a: payload.ctRating, pzem_variant: 'pzem004t-v4-classic-candidate', expires_minutes: 15 }) });
  const refreshBranches = () => { void queryClient.invalidateQueries({ queryKey: ['circuits'] }); void queryClient.invalidateQueries({ queryKey: ['devices'] }); void queryClient.invalidateQueries({ queryKey: ['home'] }); void queryClient.invalidateQueries({ queryKey: ['billing'] }); };
  const saveBranch = useMutation({
    mutationFn: (payload: { name: string; description: string | null; purpose: 'electrical_section' | 'whole_home_total'; billingSource: boolean; deviceIds: string[] }) => editingBranch
      ? api.updateCircuit(editingBranch.id, { name: payload.name, description: payload.description, purpose: payload.purpose, is_home_total: payload.purpose === 'whole_home_total', is_billing_source: payload.billingSource, device_ids: payload.deviceIds, confirmation: 'I VERIFIED THESE NON-OVERLAPPING METERS' })
      : api.createCircuit({ home_id: activeHomeId, name: payload.name, description: payload.description, purpose: payload.purpose, is_home_total: payload.purpose === 'whole_home_total', is_billing_source: payload.billingSource, device_ids: payload.deviceIds, confirmation: 'I VERIFIED THESE NON-OVERLAPPING METERS' }),
    onSuccess: () => { setBranchOpen(false); setEditingBranch(undefined); refreshBranches(); },
  });
  const removeBranch = useMutation({ mutationFn: () => api.deleteCircuit(deleteBranch?.id ?? ''), onSuccess: () => { setDeleteBranch(undefined); refreshBranches(); } });
  function submitEnrollment(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!scopedEnrollmentHomeId) return; const form = new FormData(event.currentTarget); enrollment.mutate({ friendlyName: formString(form, 'friendlyName'), ctRating: formString(form, 'ctRating'), homeId: scopedEnrollmentHomeId }); }
  function submitBranch(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!activeHomeId || branchDevices.length === 0) return; const form = new FormData(event.currentTarget); saveBranch.mutate({ name: formString(form, 'branchName'), description: formString(form, 'branchDescription') || null, purpose: branchPurpose, billingSource: branchBillingSource, deviceIds: branchDevices }); }
  function closeEnrollment() { enrollment.reset(); setEnrollmentHomeId(''); setEnrollOpen(false); }
  function openNewBranch() { setEditingBranch(undefined); setBranchDevices([]); setBranchPurpose('electrical_section'); setBranchBillingSource(false); setBranchOpen(true); }
  function openBranch(branch: Circuit) { setEditingBranch(branch); setBranchDevices(branch.device_ids); setBranchPurpose(branch.is_home_total || branch.purpose === 'whole_home_total' ? 'whole_home_total' : 'electrical_section'); setBranchBillingSource(Boolean(branch.is_billing_source)); setBranchOpen(true); }
  if (loading) return <Card title="Sensors"><Loading /></Card>;
  if (error) return <ErrorState error={error} />;
  return <>
    {mode === 'sensors' ? <><Card title="Sensors" eyebrow="Connected power sensors" action={<PermissionGate permission="sensors.enroll"><button type="button" className="button button-primary" onClick={() => { setEnrollmentHomeId(onlyHomeId ?? ''); setEnrollOpen(true); }}><Wifi aria-hidden="true" /> Enroll sensor</button></PermissionGate>}>
      {devices.length === 0 ? <EmptyState title="No sensors" detail={homeScopes.length > 0 ? 'No sensors are enrolled yet. Create a one-time enrollment token to add the first sensor.' : 'Choose a home before enrolling a sensor.'} /> : <div className="settings-list">{devices.map((device) => <button type="button" key={device.id} onClick={() => setSelected(device)}><span className="settings-list-icon"><Wifi aria-hidden="true" /></span><div><strong>{device.friendly_name}</strong><small>Last contact {timeAgo(device.heartbeat_at)}</small></div><StatusPill state={device.heartbeat_at ? 'online' : 'offline'} /><ChevronRight aria-hidden="true" /></button>)}</div>}
    </Card>{activeHomeId && <TelemetrySettings homeId={activeHomeId} />}<SensorDrawer device={selected} open={Boolean(selected)} onClose={() => setSelected(undefined)} /></> : <Card title="Main service billing source" eyebrow="Rates & data sources" action={<PermissionGate permission="sensors.configure"><button type="button" className="button button-secondary" onClick={openNewBranch} disabled={scopedDevices.length === 0}><Activity aria-hidden="true" /> Add service branch</button></PermissionGate>}><p>Select and verify the non-overlapping whole-home sensor branch used for billing calculations.</p><div className="aggregate-list">{circuits.isLoading ? <small>Loading service branches…</small> : circuits.data?.circuits.length ? circuits.data.circuits.map((branch) => <div className="service-branch-row" key={branch.id}><div><strong>{branch.name}</strong><small>{branch.is_billing_source ? 'Main service · billing source' : branch.is_home_total ? 'Whole-home total' : 'Electrical section'} · {branch.device_ids.length} sensor{branch.device_ids.length === 1 ? '' : 's'}</small></div><PermissionGate permission="sensors.configure"><button type="button" className="button button-secondary" onClick={() => openBranch(branch)}>Manage</button></PermissionGate></div>) : <small>No service branches have been added.</small>}</div></Card>}
    {mode === 'sensors' && <Dialog open={enrollOpen} title="Create one-time sensor enrollment" description="The token is short-lived, single-use, and shown only in this browser dialog." onClose={closeEnrollment}>
      {!enrollment.data && <Notice>New one-CT sensors start with energy charges only and are not added to Main service automatically.</Notice>}
      {enrollment.data ? <div className="enrollment-token"><Notice kind="warning">Copy this token into the physical USB provisioning workflow now. It cannot be shown again after this dialog closes.</Notice><code>{enrollment.data.token}</code><p>Expires {dateTime(enrollment.data.expires_at)}</p><button type="button" className="button button-primary" onClick={closeEnrollment}>I saved the token</button></div> : <form className="settings-form" onSubmit={submitEnrollment}>{homeScopes.length === 1 && <Notice>Enrollment home: {homeScopes[0]!.name}</Notice>}{homeScopes.length === 0 && <Notice kind="warning">No authorized sensor home scope is available. Choose a home before creating a token.</Notice>}<div className="field"><label htmlFor="enroll-friendly-name">Friendly name</label><input id="enroll-friendly-name" name="friendlyName" required maxLength={120} /></div><div className="field"><label htmlFor="enroll-ct-rating">CT rating (A)</label><input id="enroll-ct-rating" name="ctRating" type="number" min="1" max="1000" step="0.1" defaultValue="100" required /></div>{enrollment.isError && <Notice kind="warning">{enrollment.error instanceof Error ? enrollment.error.message : 'Enrollment token creation failed.'}</Notice>}<div className="dialog-actions"><button type="button" className="button button-secondary" onClick={closeEnrollment}>Cancel</button><button type="submit" className="button button-primary" disabled={enrollment.isPending || !scopedEnrollmentHomeId}>{enrollment.isPending ? 'Creating…' : 'Create token'}</button></div></form>}
    </Dialog>}
    {mode === 'billing' && <Dialog open={branchOpen} title={editingBranch ? `Manage ${editingBranch.name}` : 'Add service branch'} description="A service branch groups sensors that measure one electrical section. Sensors that measure the same electricity must not be added together." onClose={() => setBranchOpen(false)}>
      <form className="settings-form" onSubmit={submitBranch}><div className="field"><label htmlFor="branch-name">Service branch name</label><input id="branch-name" name="branchName" required maxLength={120} defaultValue={editingBranch?.name ?? ''} placeholder="Main service" /></div><div className="field"><label htmlFor="branch-description">Description</label><textarea id="branch-description" name="branchDescription" maxLength={500} rows={2} defaultValue={editingBranch?.description ?? ''} /></div><div className="field"><label htmlFor="branch-purpose">Purpose</label><select id="branch-purpose" value={branchPurpose} onChange={(event) => { const next = event.target.value as typeof branchPurpose; setBranchPurpose(next); if (next !== 'whole_home_total') setBranchBillingSource(false); }}><option value="electrical_section">Electrical section</option><option value="whole_home_total">Whole-home total</option></select></div>{branchPurpose === 'whole_home_total' && <label className="checkbox-row"><input type="checkbox" checked={branchBillingSource} onChange={(event) => setBranchBillingSource(event.target.checked)} /><span><strong>Use as Main service and billing source</strong><small>Dashboard, History and billing default to this confirmed whole-home branch.</small></span></label>}<fieldset><legend>Included sensors</legend><div className="permission-grid">{scopedDevices.map((device) => <label key={device.id}><input type="checkbox" checked={branchDevices.includes(device.id)} onChange={(event) => setBranchDevices((current) => event.target.checked ? [...current, device.id] : current.filter((id) => id !== device.id))} /><span><strong>{device.friendly_name}</strong><small>{device.location ?? 'Location not set'}</small></span></label>)}</div></fieldset><div className="field"><label htmlFor="branch-confirmation">Type I VERIFIED THESE NON-OVERLAPPING METERS</label><input id="branch-confirmation" required pattern="I VERIFIED THESE NON-OVERLAPPING METERS" autoComplete="off" /></div><Notice kind="warning">Only sensors confirmed to measure separate electricity can be added. This prevents the same usage from being counted twice.</Notice>{saveBranch.isError && <Notice kind="warning">{saveBranch.error instanceof Error ? saveBranch.error.message : 'The service branch could not be saved.'}</Notice>}<div className="dialog-actions">{editingBranch && <button type="button" className="button button-danger" disabled={Boolean(editingBranch.is_billing_source)} title={editingBranch.is_billing_source ? 'Choose another Main service billing source before deleting this branch.' : undefined} onClick={() => setDeleteBranch(editingBranch)}><Trash2 aria-hidden="true" /> Delete</button>}<button type="button" className="button button-secondary" onClick={() => setBranchOpen(false)}>Cancel</button><button type="submit" className="button button-primary" disabled={saveBranch.isPending || branchDevices.length === 0}>{saveBranch.isPending ? 'Saving…' : 'Save service branch'}</button></div>{editingBranch?.is_billing_source && <Notice>Choose and save a replacement Main service billing source before deleting this branch.</Notice>}</form>
    </Dialog>}
    {mode === 'billing' && <ConfirmDialog open={Boolean(deleteBranch)} title={`Delete ${deleteBranch?.name ?? 'this service branch'}?`} description="The named grouping will be removed. Sensor readings and sensor enrollment are preserved." confirmLabel="Delete service branch" busy={removeBranch.isPending} onCancel={() => setDeleteBranch(undefined)} onConfirm={() => removeBranch.mutate()} tone="danger" />}
  </>;
}

function TelemetrySettings({ homeId }: { homeId: string }) {
  const queryClient = useQueryClient();
  type TelemetryUpdate = { telemetry_interval_seconds: 2 | 5 | 10 | 15 | 30 | 60; history_interval_seconds: 15 | 30 | 60 | 300 | 900; retention_days: 30 | 90 | 180 | 365 | null; retention_confirmation?: 'DELETE EXPIRED SAVED HISTORY' };
  const [pendingRetention, setPendingRetention] = useState<TelemetryUpdate>();
  const [retentionConfirmation, setRetentionConfirmation] = useState('');
  const query = useQuery({ queryKey: ['telemetry-settings', homeId], queryFn: () => api.telemetrySettings(homeId) });
  const update = useMutation({
    mutationFn: (payload: TelemetryUpdate) => api.updateTelemetrySettings(homeId, payload),
    onSuccess: () => { setPendingRetention(undefined); setRetentionConfirmation(''); void queryClient.invalidateQueries({ queryKey: ['telemetry-settings', homeId] }); },
  });
  const authoritativeHistoryInterval = query.data?.history_interval_seconds;
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (authoritativeHistoryInterval === undefined) return;
    const form = new FormData(event.currentTarget);
    const retention = formString(form, 'retentionDays');
    const payload: TelemetryUpdate = { telemetry_interval_seconds: Number(formString(form, 'telemetryInterval')) as 2 | 5 | 10 | 15 | 30 | 60, history_interval_seconds: authoritativeHistoryInterval, retention_days: retention === '' ? null : Number(retention) as 30 | 90 | 180 | 365 };
    const shortensRetention = payload.retention_days !== null && (query.data?.retention_days === null || (query.data?.retention_days !== undefined && payload.retention_days < query.data.retention_days));
    if (shortensRetention) { setPendingRetention(payload); setRetentionConfirmation(''); return; }
    update.mutate(payload);
  }
  if (query.isLoading) return <Card title="Reading schedule & retention"><Loading /></Card>;
  if (query.isError || !query.data) return <Card title="Reading schedule & retention"><Notice>This server does not yet provide stateless sensor schedule settings.</Notice></Card>;
  return <><Card title="Reading schedule & retention" eyebrow="Server-managed history"><form className="settings-form" onSubmit={submit}>
    <div className="filter-row"><div className="field"><label htmlFor="telemetry-interval">Live reading interval</label><select id="telemetry-interval" name="telemetryInterval" defaultValue={String(query.data.telemetry_interval_seconds)}>{[2, 5, 10, 15, 30, 60].map((value) => <option key={value} value={value}>{value} seconds</option>)}</select></div><div className="field"><span>History interval</span><strong>{query.data.history_interval_seconds < 60 ? `${query.data.history_interval_seconds} seconds` : `${query.data.history_interval_seconds / 60} minutes`}</strong><small>Managed in <a href="/settings?section=rates">Rates &amp; data sources</a>.</small></div><div className="field"><label htmlFor="retention-days">History retention</label><select id="retention-days" name="retentionDays" defaultValue={query.data.retention_days === null ? '' : String(query.data.retention_days)}><option value="">Keep until an administrator removes it</option>{[30, 90, 180, 365].map((value) => <option key={value} value={value}>{value} days</option>)}</select></div></div>
    <Notice>Sensors send current measurements directly to the server. History retention is managed here and does not depend on removable sensor storage.</Notice>
    {update.isSuccess && <Notice kind="success">Reading schedule and retention were saved.</Notice>}{update.isError && <Notice kind="warning">{update.error instanceof Error ? update.error.message : 'Reading settings could not be saved.'}</Notice>}
    <PermissionGate permission="system.manage"><button type="submit" className="button button-primary" disabled={update.isPending}>{update.isPending ? 'Saving…' : 'Save reading settings'}</button></PermissionGate>
  </form></Card><ConfirmDialog open={Boolean(pendingRetention)} title="Shorten saved History retention?" description={<div><p>Saved History older than the new limit will be permanently removed only for this home. Sensor identity, current readings and rate plans are preserved.</p><div className="field"><label htmlFor="retention-confirmation">Type DELETE EXPIRED SAVED HISTORY</label><input id="retention-confirmation" value={retentionConfirmation} onChange={(event) => setRetentionConfirmation(event.target.value)} autoComplete="off" /></div></div>} confirmLabel="Shorten retention" confirmDisabled={retentionConfirmation !== 'DELETE EXPIRED SAVED HISTORY'} busy={update.isPending} onCancel={() => { setPendingRetention(undefined); setRetentionConfirmation(''); }} onConfirm={() => { if (pendingRetention && retentionConfirmation === 'DELETE EXPIRED SAVED HISTORY') update.mutate({ ...pendingRetention, retention_confirmation: 'DELETE EXPIRED SAVED HISTORY' }); }} tone="danger" /></>;
}

function UserSettings({ users, roles, loading, error }: { users: UserType[]; roles: Array<{ id: string; name: string; permissions: string[]; built_in: boolean }>; loading: boolean; error: unknown }) {
  const { can } = useSession();
  const queryClient = useQueryClient();
  const profile = useQuery({ queryKey: ['profile'], queryFn: api.profile });
  const [selected, setSelected] = useState<UserType>();
  const [selectedRoles, setSelectedRoles] = useState<string[]>([]);
  const [selectedEnabled, setSelectedEnabled] = useState(true);
  const [pendingUpdate, setPendingUpdate] = useState<{ email: string; display_name: string; role_names: string[]; enabled: boolean }>();
  const [resetPassword, setResetPassword] = useState('');
  const [resetConfirmOpen, setResetConfirmOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<UserType>();
  const [addOpen, setAddOpen] = useState(false);
  const update = useMutation({ mutationFn: (payload: { email: string; display_name: string; role_names: string[]; enabled: boolean }) => api.updateUser(selected?.id ?? '', payload), onSuccess: () => { setPendingUpdate(undefined); setSelected(undefined); void queryClient.invalidateQueries({ queryKey: ['users'] }); } });
  const reset = useMutation({ mutationFn: () => api.resetUserPassword(selected?.id ?? '', resetPassword), onSuccess: () => { setResetConfirmOpen(false); setResetPassword(''); setSelected(undefined); void queryClient.invalidateQueries({ queryKey: ['users'] }); } });
  const remove = useMutation({ mutationFn: () => api.deleteUser(deleteTarget?.id ?? ''), onSuccess: () => { setDeleteTarget(undefined); setSelected(undefined); void queryClient.invalidateQueries({ queryKey: ['users'] }); } });
  const restore = useMutation({ mutationFn: (id: string) => api.restoreUser(id), onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['users'] }) });
  const create = useMutation({ mutationFn: (payload: { email: string; displayName: string; password: string; roles: string[] }) => api.createUser({ email: payload.email, display_name: payload.displayName, password: payload.password, role_names: payload.roles }), onSuccess: () => { setAddOpen(false); void queryClient.invalidateQueries({ queryKey: ['users'] }); } });
  const selfUpdate = useMutation({ mutationFn: (payload: { display_name?: string; email?: string; current_password?: string }) => api.updateProfile(payload), onSuccess: (value) => { void queryClient.invalidateQueries({ queryKey: ['profile'] }); void queryClient.invalidateQueries({ queryKey: ['session'] }); if (value.session_revoked) window.location.reload(); } });
  const password = useMutation({ mutationFn: (payload: { current: string; next: string }) => api.changePassword(payload.current, payload.next), onSuccess: () => window.location.reload() });
  function openUser(user: UserType) { setSelected(user); setSelectedRoles(user.roles); setSelectedEnabled(user.enabled); setResetPassword(''); setResetConfirmOpen(false); }
  function submitNewUser(event: FormEvent<HTMLFormElement>) { event.preventDefault(); const form = new FormData(event.currentTarget); create.mutate({ email: formString(form, 'email'), displayName: formString(form, 'displayName'), password: formString(form, 'password'), roles: form.getAll('roles').map(String) }); }
  function submitAdminUpdate(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!selected) return; const form = new FormData(event.currentTarget); setPendingUpdate({ email: formString(form, 'email'), display_name: formString(form, 'displayName'), role_names: selectedRoles, enabled: selectedEnabled }); }
  function submitProfile(event: FormEvent<HTMLFormElement>) { event.preventDefault(); const form = new FormData(event.currentTarget); const email = formString(form, 'email'); selfUpdate.mutate({ display_name: formString(form, 'displayName'), ...(email !== profile.data?.email ? { email, current_password: formString(form, 'currentPassword') } : {}) }); }
  function submitPassword(event: FormEvent<HTMLFormElement>) { event.preventDefault(); const form = new FormData(event.currentTarget); const next = formString(form, 'newPassword'); const confirm = formString(form, 'confirmPassword'); const confirmationInput = event.currentTarget.elements.namedItem('confirmPassword'); if (confirmationInput instanceof HTMLInputElement) confirmationInput.setCustomValidity(next === confirm ? '' : 'Passwords do not match.'); if (next !== confirm) { event.currentTarget.reportValidity(); return; } password.mutate({ current: formString(form, 'currentPassword'), next }); }
  return <>
    <Card title="Your profile" eyebrow="Personal identity and security">
      {profile.isLoading && <Loading label="Loading profile" />}
      {profile.isError && <ErrorState error={profile.error} retry={() => void profile.refetch()} />}
      {profile.data && <div className="settings-split"><form className="settings-form" onSubmit={submitProfile}><div className="field"><label htmlFor="profile-name">Display name</label><input id="profile-name" name="displayName" defaultValue={profile.data.display_name} required maxLength={120} autoComplete="name" /></div><div className="field"><label htmlFor="profile-email">Email</label><input id="profile-email" name="email" type="email" defaultValue={profile.data.email} required autoComplete="username" /></div><div className="field"><label htmlFor="profile-current-password">Current password (required when changing email)</label><input id="profile-current-password" name="currentPassword" type="password" autoComplete="current-password" /></div><p className="disclosure">Role: {profile.data.roles.join(', ') || 'No role'} · Status: {profile.data.enabled ? 'Enabled' : 'Disabled'}</p>{selfUpdate.isError && <Notice kind="warning">{selfUpdate.error instanceof Error ? selfUpdate.error.message : 'Profile update failed.'}</Notice>}<button className="button button-primary" type="submit" disabled={selfUpdate.isPending}>{selfUpdate.isPending ? 'Saving…' : 'Save profile'}</button></form><form className="settings-form" onSubmit={submitPassword}><div className="field"><label htmlFor="password-current">Current password</label><input id="password-current" name="currentPassword" type="password" required autoComplete="current-password" /></div><div className="field"><label htmlFor="password-new">New password</label><input id="password-new" name="newPassword" type="password" minLength={14} required autoComplete="new-password" /></div><div className="field"><label htmlFor="password-confirm">Confirm new password</label><input id="password-confirm" name="confirmPassword" type="password" minLength={14} required autoComplete="new-password" /></div><small>Use at least 14 characters. Changing it signs out every active session.</small>{password.isError && <Notice kind="warning">{password.error instanceof Error ? password.error.message : 'Password change failed.'}</Notice>}<button className="button button-secondary" type="submit" disabled={password.isPending}><KeyRound aria-hidden="true" /> {password.isPending ? 'Changing…' : 'Change password'}</button></form></div>}
    </Card>
    {can('users.view') && <Card title="Users & access" eyebrow="Server-enforced roles and granular permissions" action={can('users.manage') ? <button type="button" className="button button-primary" onClick={() => setAddOpen(true)}><UserPlus aria-hidden="true" /> Add user</button> : undefined}>{loading ? <Loading /> : error ? <ErrorState error={error} /> : <div className="settings-list">{users.map((user) => <button type="button" key={user.id} onClick={() => openUser(user)}><span className="settings-list-icon"><Shield aria-hidden="true" /></span><div><strong>{user.display_name}</strong><small>{user.email} · {user.roles.join(', ') || 'No role'} · created {user.created_at ? dateTime(user.created_at) : 'unknown'} · last login {user.last_login_at ? dateTime(user.last_login_at) : 'never'}</small></div><StatusPill state={user.deleted_at ? 'offline' : user.enabled ? 'online' : 'offline'} label={user.deleted_at ? 'Deleted' : user.enabled ? 'Enabled' : 'Disabled'} /><ChevronRight aria-hidden="true" /></button>)}</div>}</Card>}
    <Dialog open={Boolean(selected)} title={`Manage ${selected?.display_name ?? ''}`} description="Changes are enforced on the server. Sensitive changes revoke affected sessions; the final enabled Owner remains protected." onClose={() => setSelected(undefined)}>{selected && <form className="settings-form" onSubmit={submitAdminUpdate}><div className="field"><label htmlFor="manage-user-name">Display name</label><input id="manage-user-name" name="displayName" defaultValue={selected.display_name} required maxLength={120} disabled={!can('users.manage') || !selected.manageable} /></div><div className="field"><label htmlFor="manage-user-email">Email</label><input id="manage-user-email" name="email" type="email" defaultValue={selected.email} required disabled={!can('users.manage') || !selected.manageable} /></div><label className="account-enabled"><input type="checkbox" checked={selectedEnabled} onChange={(event) => setSelectedEnabled(event.target.checked)} disabled={!can('users.manage') || !selected.manageable || Boolean(selected.deleted_at)} /><span><strong>Account enabled</strong><small>Disabling revokes every active session.</small></span></label><div className="permission-grid">{roles.map((role) => <label key={role.id}><input type="checkbox" checked={selectedRoles.includes(role.name)} disabled={!can('users.manage') || !selected.manageable || Boolean(selected.deleted_at)} onChange={(event) => setSelectedRoles((current) => event.target.checked ? [...current, role.name] : current.filter((entry) => entry !== role.name))} /><span><strong>{role.name}</strong><small>{role.permissions.length} permissions</small></span></label>)}</div>{update.isError && <Notice kind="warning">{isForbidden(update.error) ? 'The server refused this account change.' : update.error instanceof Error ? update.error.message : 'Account update failed.'}</Notice>}<div className="field"><label htmlFor="admin-reset-password">Replacement password</label><input id="admin-reset-password" type="password" minLength={14} value={resetPassword} onChange={(event) => setResetPassword(event.target.value)} autoComplete="new-password" disabled={!can('users.manage') || !selected.manageable || Boolean(selected.deleted_at)} /><small>Never reveals the existing password. A reset revokes every session.</small></div><div className="dialog-actions"><button type="button" className="button button-secondary" onClick={() => setSelected(undefined)}>Close</button>{selected.deleted_at ? <button type="button" className="button button-primary" onClick={() => restore.mutate(selected.id)} disabled={restore.isPending}><RotateCcw aria-hidden="true" /> Restore</button> : <>{can('users.manage') && <button type="button" className="button button-secondary" onClick={() => setResetConfirmOpen(true)} disabled={reset.isPending || resetPassword.length < 14}><KeyRound aria-hidden="true" /> Reset password</button>} {can('users.manage') && <button type="button" className="button button-secondary" onClick={() => setDeleteTarget(selected)} disabled={!selected.manageable}><Trash2 aria-hidden="true" /> Delete</button>} {can('users.manage') && <button type="submit" className="button button-primary" disabled={update.isPending || selectedRoles.length === 0 || !selected.manageable}>{update.isPending ? 'Saving…' : 'Review changes'}</button>}</>}</div></form>}</Dialog>
    <ConfirmDialog open={Boolean(pendingUpdate)} title="Apply account and access changes?" description="Role, enablement, name, and email changes take effect immediately. Security-sensitive changes revoke active sessions." confirmLabel="Apply changes" busy={update.isPending} onCancel={() => setPendingUpdate(undefined)} onConfirm={() => { if (pendingUpdate) update.mutate(pendingUpdate); }} tone="warning" />
    <ConfirmDialog open={Boolean(deleteTarget)} title={`Delete ${deleteTarget?.display_name ?? 'this user'}?`} description="The account is soft-deleted, disabled, and signed out. Audit history is preserved. The last enabled Owner cannot be deleted." confirmLabel="Delete user" busy={remove.isPending} onCancel={() => setDeleteTarget(undefined)} onConfirm={() => remove.mutate()} tone="danger" />
    <ConfirmDialog open={resetConfirmOpen} title={`Reset ${selected?.display_name ?? 'this user'}'s password?`} description="The replacement password takes effect immediately and every existing session for this account is revoked. The previous password is never revealed." confirmLabel="Reset password" busy={reset.isPending} onCancel={() => setResetConfirmOpen(false)} onConfirm={() => reset.mutate()} tone="warning" />
    <Dialog open={addOpen} title="Add a local user" description="Create a local account and assign one or more server-enforced roles." onClose={() => setAddOpen(false)}><form className="settings-form" onSubmit={submitNewUser}><div className="field"><label htmlFor="new-user-name">Display name</label><input id="new-user-name" name="displayName" required maxLength={120} autoComplete="name" /></div><div className="field"><label htmlFor="new-user-email">Email</label><input id="new-user-email" name="email" type="email" required autoComplete="username" /></div><div className="field"><label htmlFor="new-user-password">Initial password</label><input id="new-user-password" name="password" type="password" required minLength={14} autoComplete="new-password" /></div><fieldset><legend>Roles</legend><div className="permission-grid">{roles.map((role) => <label key={role.id}><input type="checkbox" name="roles" value={role.name} /><span><strong>{role.name}</strong><small>{role.permissions.length} permissions</small></span></label>)}</div></fieldset>{create.isError && <Notice kind="warning">{create.error instanceof Error ? create.error.message : 'User creation failed.'}</Notice>}<div className="dialog-actions"><button type="button" className="button button-secondary" onClick={() => setAddOpen(false)}>Cancel</button><button type="submit" className="button button-primary" disabled={create.isPending}>{create.isPending ? 'Creating…' : 'Create user'}</button></div></form></Dialog>
  </>;
}

function BillingConfigurationSettings({ homeId }: { homeId: string }) {
  const queryClient = useQueryClient();
  const { can } = useSession();
  const query = useQuery({ queryKey: ['home-utility', homeId], queryFn: () => api.homeUtility(homeId), enabled: Boolean(homeId) });
  const [scopeOverride, setScopeOverride] = useState<string>();
  const update = useMutation({
    mutationFn: (payload: Record<string, unknown>) => api.updateHomeUtility(homeId, payload),
    onSuccess: () => { setScopeOverride(undefined); void queryClient.invalidateQueries({ queryKey: ['home-utility', homeId] }); void queryClient.invalidateQueries({ queryKey: ['billing', homeId] }); void queryClient.invalidateQueries({ queryKey: ['home', homeId] }); },
  });
  if (!homeId) return <EmptyState title="Choose an active home" detail="Select a home before loading billing configuration." />;
  if (query.isLoading) return <Card title="Billing configuration"><Loading label="Loading billing configuration" /></Card>;
  if (query.isError || !query.data) return <ErrorState error={query.error ?? new Error('Billing configuration is unavailable.')} retry={() => void query.refetch()} />;
  const utility = query.data.utility;
  const scope = scopeOverride ?? utility.cost_scope;
  const writable = can('system.manage') && (query.data.billing_configuration_writable ?? true);
  const nullable = (form: FormData, name: string) => formString(form, name) || null;
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    update.mutate({
      timezone: formString(form, 'timezone'), billing_day: Number(formString(form, 'billingDay')), cost_scope: scope,
      baseline_allocation_kwh: nullable(form, 'baselineAllocation'), cca_provider: nullable(form, 'ccaProvider'), currency: formString(form, 'currency'),
      generation_service_kind: formString(form, 'generationServiceKind'), baseline_region: nullable(form, 'baselineRegion'),
      summer_baseline_kwh_per_day: nullable(form, 'summerBaseline'), winter_baseline_kwh_per_day: nullable(form, 'winterBaseline'),
      all_electric: form.has('allElectric'), medical_baseline: form.has('medicalBaseline'), heat_pump_allocation: form.has('heatPumpAllocation'),
      estimate_high_coverage: formString(form, 'estimateHighCoverage'), estimate_min_coverage: formString(form, 'estimateMinCoverage'),
      max_estimatable_gap_seconds: Number(formString(form, 'maxEstimatableGap')), billing_history_interval_seconds: Number(formString(form, 'billingHistoryInterval')),
      projection_minimum_hours: Number(formString(form, 'projectionMinimumHours')),
      ...(scope === 'full_account' ? { full_account_confirmation: formString(form, 'fullAccountConfirmation') } : {}),
      ...(scope === 'allocated_account' ? { allocated_account_confirmation: formString(form, 'allocatedAccountConfirmation') } : {}),
    });
  }
  return <Card title="Utility and billing calculation" eyebrow="Rates & data sources"><form key={`${query.data.home.id}-${String(scope)}`} className="settings-form billing-configuration-form" onSubmit={submit}>
    <dl><div><dt>Utility provider</dt><dd>{utility.utility_name}</dd></div><div><dt>Configuration owner</dt><dd>Rates &amp; data sources</dd></div></dl>
    <div className="filter-row"><div className="field"><label htmlFor="billing-timezone">Home timezone</label><input id="billing-timezone" name="timezone" defaultValue={query.data.home.timezone} required maxLength={80} disabled={!writable} /></div><div className="field"><label htmlFor="billing-cycle-day">Billing-cycle start day</label><input id="billing-cycle-day" name="billingDay" type="number" min={1} max={28} defaultValue={utility.billing_day} required disabled={!writable} /></div><div className="field"><label htmlFor="billing-currency">Currency</label><input id="billing-currency" name="currency" pattern="[A-Z]{3}" maxLength={3} defaultValue={utility.currency} required disabled={!writable} /></div></div>
    <div className="filter-row"><div className="field"><label htmlFor="billing-generation-kind">Generation provider type</label><select id="billing-generation-kind" name="generationServiceKind" defaultValue={utility.generation_service_kind} disabled={!writable}><option value="sce_generation">SCE generation</option><option value="cca">Community Choice Aggregation</option><option value="direct_access">Direct Access</option><option value="unknown">Unknown</option></select></div><div className="field"><label htmlFor="billing-cca-provider">CCA or Direct Access provider</label><input id="billing-cca-provider" name="ccaProvider" defaultValue={utility.cca_provider ?? ''} maxLength={120} disabled={!writable} /></div><div className="field"><label htmlFor="billing-cost-scope">Billing cost scope</label><select id="billing-cost-scope" name="costScope" value={scope} onChange={(event) => setScopeOverride(event.target.value)} disabled={!writable}><option value="energy_only">Energy charges only</option><option value="allocated_account">Allocated account</option><option value="full_account">Full account</option></select></div></div>
    <fieldset><legend>Home baseline</legend><div className="filter-row"><div className="field"><label htmlFor="billing-baseline-region">Climate / baseline region</label><input id="billing-baseline-region" name="baselineRegion" defaultValue={utility.baseline_region ?? ''} maxLength={80} disabled={!writable} /></div><div className="field"><label htmlFor="billing-summer-baseline">Summer baseline (kWh/day)</label><input id="billing-summer-baseline" name="summerBaseline" type="number" min={0} step="0.001" defaultValue={utility.summer_baseline_kwh_per_day === null ? '' : String(utility.summer_baseline_kwh_per_day)} disabled={!writable} /></div><div className="field"><label htmlFor="billing-winter-baseline">Winter baseline (kWh/day)</label><input id="billing-winter-baseline" name="winterBaseline" type="number" min={0} step="0.001" defaultValue={utility.winter_baseline_kwh_per_day === null ? '' : String(utility.winter_baseline_kwh_per_day)} disabled={!writable} /></div></div><div className="field"><label htmlFor="billing-baseline-allocation">Verified baseline allocation (kWh)</label><input id="billing-baseline-allocation" name="baselineAllocation" type="number" min={0} step="0.001" defaultValue={utility.baseline_allocation_kwh === null ? '' : String(utility.baseline_allocation_kwh)} disabled={!writable} /></div><div className="permission-grid"><label><input type="checkbox" name="allElectric" defaultChecked={utility.all_electric} disabled={!writable} /><span><strong>All-electric home</strong></span></label><label><input type="checkbox" name="medicalBaseline" defaultChecked={utility.medical_baseline} disabled={!writable} /><span><strong>Medical Baseline</strong></span></label><label><input type="checkbox" name="heatPumpAllocation" defaultChecked={utility.heat_pump_allocation} disabled={!writable} /><span><strong>Heat-pump allocation</strong></span></label></div></fieldset>
    <fieldset><legend>Billing estimate policy</legend><div className="filter-row"><div className="field"><label htmlFor="billing-high-coverage">High-confidence coverage</label><input id="billing-high-coverage" name="estimateHighCoverage" type="number" min={0} max={1} step="0.001" defaultValue={String(utility.estimate_high_coverage)} disabled={!writable} /></div><div className="field"><label htmlFor="billing-min-coverage">Minimum estimate coverage</label><input id="billing-min-coverage" name="estimateMinCoverage" type="number" min={0} max={1} step="0.001" defaultValue={String(utility.estimate_min_coverage)} disabled={!writable} /></div><div className="field"><label htmlFor="billing-max-gap">Maximum estimatable gap (seconds)</label><input id="billing-max-gap" name="maxEstimatableGap" type="number" min={60} max={86400} defaultValue={utility.max_estimatable_gap_seconds} disabled={!writable} /></div></div><div className="filter-row"><div className="field"><label htmlFor="billing-history-interval">History interval used for billing</label><select id="billing-history-interval" name="billingHistoryInterval" defaultValue={utility.billing_history_interval_seconds} disabled={!writable}>{[15, 30, 60, 300, 900].map((value) => <option key={value} value={value}>{value < 60 ? `${value} seconds` : `${value / 60} minutes`}</option>)}</select></div><div className="field"><label htmlFor="billing-projection-hours">Projection minimum data (hours)</label><input id="billing-projection-hours" name="projectionMinimumHours" type="number" min={1} max={720} defaultValue={utility.projection_minimum_hours} disabled={!writable} /></div><div className="field"><span>Billing calculation evidence</span><strong>Retained indefinitely</strong><small>Immutable calculation evidence is read-only and cannot be shortened.</small></div></div></fieldset>
    {scope === 'full_account' && <div className="field"><label htmlFor="billing-full-confirmation">Type I UNDERSTAND FULL ACCOUNT SCOPE</label><input id="billing-full-confirmation" name="fullAccountConfirmation" required pattern="I UNDERSTAND FULL ACCOUNT SCOPE" autoComplete="off" disabled={!writable} /></div>}
    {scope === 'allocated_account' && <div className="field"><label htmlFor="billing-allocated-confirmation">Type I VERIFIED THIS ALLOCATION SCOPE</label><input id="billing-allocated-confirmation" name="allocatedAccountConfirmation" required pattern="I VERIFIED THIS ALLOCATION SCOPE" autoComplete="off" disabled={!writable} /></div>}
    <Notice>Usage remains derived only from authenticated sensor readings. Estimates never modify accepted History.</Notice>{update.isError && <Notice kind="warning">{update.error instanceof Error ? update.error.message : 'Billing configuration could not be saved.'}</Notice>}{update.isSuccess && <Notice kind="success">Billing configuration was saved.</Notice>}{writable && <button type="submit" className="button button-primary" disabled={update.isPending}>{update.isPending ? 'Saving…' : 'Save billing configuration'}</button>}
  </form></Card>;
}

function RateSettings({ homeId }: { homeId: string }) {
  return <><BillingConfigurationSettings homeId={homeId} /><BillingPage mode="settings" /></>;
}

function otaStageLabel(state: string) {
  return ({
    staged: 'Waiting to start', queued: 'Waiting for sensor', downloading: 'Downloading or installing',
    rebooting: 'Restarting', validating: 'Confirming version', succeeded: 'Updated', failed: 'Update failed',
    rolled_back: 'Rolled back', timed_out: 'Timed out', cancelled: 'Canceled', canceled: 'Canceled', archived: 'Archived', deleted: 'Deleted',
  } as Record<string, string>)[state] ?? state.replaceAll('_', ' ');
}

function firmwareReleaseState(release: FirmwareRelease): 'draft' | 'validating' | 'available' | 'current' | 'archived' | 'rejected' | 'deleted' {
  if (release.release_state) return release.release_state;
  if (!release.artifact_available) return 'archived';
  return release.candidate ? 'validating' : 'available';
}

function firmwareProtectionLabel(reason: string): string {
  return ({
    current_recommended_release: 'Current recommended release',
    active_or_pending_deployment: 'An update is active or waiting',
    reported_by_sensor: 'A sensor currently reports this version',
    pinned_for_rollback: 'Pinned for rollback',
    deployment_not_terminal: 'Deployment is still active',
    troubleshooting_hold: 'Troubleshooting hold is active',
    archive_required_before_deletion: 'Archive the deployment first',
    already_deleted: 'Already deleted',
  } as Record<string, string>)[reason] ?? reason.replaceAll('_', ' ');
}

function FirmwareBatchStatus({
  batch,
  canManage,
  retryPending,
  onRetry,
  onCancel,
  onArchive,
  onRestore,
  onDelete,
}: {
  batch: FirmwareDeploymentBatch;
  canManage: boolean;
  retryPending: boolean;
  onRetry: (deviceId: string) => void;
  onCancel: () => void;
  onArchive: () => void;
  onRestore: () => void;
  onDelete: () => void;
}) {
  const canCancel = batch.jobs.some((job) => job.cancel_eligible);
  const displayState = batch.deployment_state ?? batch.state;
  const resultState = batch.result_state ?? batch.state;
  return <section className="settings-callout" aria-label={`Deployment ${batch.id}`}>
    <RefreshCw aria-hidden="true" />
    <div>
      <h3>{batch.targeted} sensors targeted · {batch.succeeded} updated · {batch.failed} failed · {batch.pending} pending</h3>
      <p>{batch.rollout === 'retry' ? 'Retry' : `${batch.rollout} rollout`}{displayState === 'archived' ? ` · archived · final result ${otaStageLabel(resultState)}` : ''} · last changed {dateTime(batch.updated_at)}</p>
      <details className="firmware-sensor-results"><summary>View sensor results</summary><div className="release-list">
        {batch.jobs.map((job) => {
          const attemptState = job.attempt_state ?? job.state;
          return <article key={job.id}>
          <Wifi aria-hidden="true" />
          <div>
            <strong>{job.device_name}</strong>
            <span>{job.current_version ?? 'version unavailable'} → {job.target_version} · {otaStageLabel(attemptState)} · {job.progress_percent}%</span>
            <small>{job.error_message ?? (job.state === 'succeeded' ? `Confirmed by heartbeat ${job.confirmation_heartbeat_at ? dateTime(job.confirmation_heartbeat_at) : ''}` : `Attempt ${job.attempt} · updated ${dateTime(job.updated_at)}`)}</small>
            {(job.error_code || job.reported_firmware_after_reboot) && <details><summary>Technical details</summary><code>{job.error_code ?? 'VERSION_EVIDENCE'}{job.reported_firmware_after_reboot ? ` · reported ${job.reported_firmware_after_reboot}` : ''}</code></details>}
          </div>
          <StatusPill state={attemptState} label={otaStageLabel(attemptState)} />
          {canManage && job.retry_eligible && <button type="button" className="button button-secondary" disabled={retryPending} onClick={() => onRetry(job.device_id)}>{retryPending ? 'Retrying…' : 'Retry sensor'}</button>}
        </article>; })}
      </div></details>
    </div>
    <StatusPill state={displayState} label={displayState === 'partial' ? 'Partially completed' : otaStageLabel(displayState)} />
    {canManage && canCancel && <button type="button" className="button button-danger" onClick={onCancel}>Cancel waiting jobs</button>}
    {canManage && batch.archive_eligible && <button type="button" className="button button-secondary" onClick={onArchive}><ArchiveRestore aria-hidden="true" /> Archive deployment</button>}
    {canManage && batch.restore_eligible && <button type="button" className="button button-secondary" onClick={onRestore}><RotateCcw aria-hidden="true" /> Restore deployment</button>}
    {canManage && batch.delete_eligibility && <button type="button" className="button button-danger" onClick={onDelete} disabled={!batch.delete_eligibility.eligible} title={batch.delete_eligibility.eligible ? undefined : batch.delete_eligibility.protection_reasons.map(firmwareProtectionLabel).join('. ')}><Trash2 aria-hidden="true" /> Delete deployment record</button>}
  </section>;
}

function FirmwareReleaseRow({ release, devices, canManage, retryPending, onDeploy, onMakeCurrent, onToggleRollback, onArchive, onRestore, onDelete, onRetry, onCancel, onArchiveBatch, onRestoreBatch, onDeleteBatch }: {
  release: FirmwareRelease;
  devices: DeviceDetail[];
  canManage: boolean;
  retryPending: boolean;
  onDeploy: () => void;
  onMakeCurrent: () => void;
  onToggleRollback: () => void;
  onArchive: () => void;
  onRestore: () => void;
  onDelete: () => void;
  onRetry: (batchId: string, deviceId: string) => void;
  onCancel: (batch: FirmwareDeploymentBatch) => void;
  onArchiveBatch: (batch: FirmwareDeploymentBatch) => void;
  onRestoreBatch: (batch: FirmwareDeploymentBatch) => void;
  onDeleteBatch: (batch: FirmwareDeploymentBatch) => void;
}) {
  const state = firmwareReleaseState(release);
  const needsReconciliation = release.consistency?.status === 'attention_required';
  const deployable = release.deploy_eligible ?? ((state === 'available' || state === 'current') && release.artifact_available);
  const deleteEligibility = release.delete_eligibility;
  const deploymentCount = release.deployment_count ?? release.deployment_batches.reduce((count, batch) => count + batch.targeted, 0);
  return <div className="firmware-release-item">
    <article className="firmware-release-row"><Cpu aria-hidden="true" /><div><strong>{release.semantic_version} · build {release.build_number}</strong><span>{release.board_profile} · {bytes(release.image_size)}</span><small>Validation {release.validation_status ?? (release.artifact_available ? 'ready' : 'unavailable')} · {deploymentCount} deployment record{deploymentCount === 1 ? '' : 's'}</small>{release.rollback_pinned && <small>Protected for rollback</small>}{needsReconciliation && <small>Lifecycle consistency needs administrator attention.</small>}{deleteEligibility && !deleteEligibility.eligible && <small>Protected: {deleteEligibility.protection_reasons.map(firmwareProtectionLabel).join(', ')}</small>}</div><StatusPill state={needsReconciliation ? 'warning' : state === 'available' || state === 'current' ? 'approved' : state === 'validating' ? 'warning' : state === 'rejected' || state === 'deleted' ? 'offline' : 'neutral'} label={needsReconciliation ? 'Needs reconciliation' : state.replaceAll('_', ' ')} />{canManage && <div className="release-actions">{deployable && !needsReconciliation && <button type="button" className="button button-primary" onClick={onDeploy} disabled={devices.length === 0}>Deploy</button>}{state === 'available' && release.artifact_available && !needsReconciliation && <button type="button" className="button button-secondary" onClick={onMakeCurrent}>Make current</button>}{((state === 'available' && release.artifact_available) || release.rollback_pinned) && <button type="button" className="button button-secondary" onClick={onToggleRollback}>{release.rollback_pinned ? 'Remove rollback protection' : 'Protect for rollback'}</button>}{release.archive_eligible && <button type="button" className="button button-secondary" onClick={onArchive}><ArchiveRestore aria-hidden="true" /> Archive release</button>}{release.restore_eligible && <button type="button" className="button button-secondary" onClick={onRestore}><RotateCcw aria-hidden="true" /> Restore release</button>}{deleteEligibility && state !== 'deleted' && <button type="button" className="button button-danger" onClick={onDelete} disabled={!deleteEligibility.eligible} title={deleteEligibility.eligible ? undefined : deleteEligibility.protection_reasons.map(firmwareProtectionLabel).join('. ')}><Trash2 aria-hidden="true" /> Delete permanently</button>}</div>}</article>
    <details className="technical-details firmware-release-technical"><summary>Technical details</summary><dl><div><dt>Project / chip</dt><dd>{release.project_name} · {release.target_chip}</dd></div><div><dt>SHA-256</dt><dd><code>{release.sha256}</code></dd></div><div><dt>Minimum protocol</dt><dd>{release.minimum_protocol}</dd></div><div><dt>Minimum boot / config</dt><dd>{release.minimum_boot_version} / {release.minimum_config_version}</dd></div><div><dt>Physical certification</dt><dd>{release.physical_certification ?? 'Not reported'}</dd></div><div><dt>Release notes</dt><dd>{release.release_notes || 'None'}</dd></div><div><dt>Release ID</dt><dd><code>{release.release_id}</code></dd></div>{release.consistency && <div><dt>Lifecycle consistency</dt><dd>{release.consistency.status === 'consistent' ? 'Consistent' : release.consistency.issues.map(firmwareProtectionLabel).join(', ')}</dd></div>}</dl></details>
    {release.deployment_batches.length > 0 && <details className="firmware-deployments"><summary>Deployment summary ({release.deployment_batches.length})</summary>{release.deployment_batches.map((batch) => <FirmwareBatchStatus key={batch.id} batch={batch} canManage={canManage} retryPending={retryPending} onRetry={(deviceId) => onRetry(batch.id, deviceId)} onCancel={() => onCancel(batch)} onArchive={() => onArchiveBatch(batch)} onRestore={() => onRestoreBatch(batch)} onDelete={() => onDeleteBatch(batch)} />)}</details>}
  </div>;
}

function FirmwareSettings({ devices, releases, reconciliation, loading, error }: { devices: DeviceDetail[]; releases: FirmwareRelease[]; reconciliation?: FirmwareReleaseList['reconciliation']; loading: boolean; error: unknown }) {
  const { can } = useSession();
  const queryClient = useQueryClient();
  const [uploadOpen, setUploadOpen] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [showDeploymentHistory, setShowDeploymentHistory] = useState(false);
  const [deployTarget, setDeployTarget] = useState<FirmwareRelease>();
  const [cancelBatchTarget, setCancelBatchTarget] = useState<FirmwareDeploymentBatch>();
  const [deleteArtifactTarget, setDeleteArtifactTarget] = useState<FirmwareRelease>();
  const [makeCurrentTarget, setMakeCurrentTarget] = useState<FirmwareRelease>();
  const [rollbackProtectionTarget, setRollbackProtectionTarget] = useState<FirmwareRelease>();
  const [archiveReleaseTarget, setArchiveReleaseTarget] = useState<FirmwareRelease>();
  const [archiveBatchTarget, setArchiveBatchTarget] = useState<FirmwareDeploymentBatch>();
  const [deleteBatchTarget, setDeleteBatchTarget] = useState<FirmwareDeploymentBatch>();
  const [deleteReleaseConfirmation, setDeleteReleaseConfirmation] = useState('');
  const [deleteBatchConfirmation, setDeleteBatchConfirmation] = useState('');
  const [retentionDraft, setRetentionDraft] = useState<string>();
  const [retentionConfirmOpen, setRetentionConfirmOpen] = useState(false);
  const [selectedDevices, setSelectedDevices] = useState<string[]>([]);
  const [rollout, setRollout] = useState<'immediate' | 'staged'>('staged');
  const [firmwareImage, setFirmwareImage] = useState<File>();
  const [firmwareManifest, setFirmwareManifest] = useState<File>();
  const [firmwareNotes, setFirmwareNotes] = useState<File>();
  const [preparedUpload, setPreparedUpload] = useState<PreparedFirmwareUpload>();
  const [preparationError, setPreparationError] = useState<string>();
  const [preparingUpload, setPreparingUpload] = useState(false);
  const preparationGeneration = useRef(0);
  const expandedFirmware = useQuery({
    queryKey: ['firmware-releases', 'expanded', showArchived, showDeploymentHistory],
    queryFn: () => api.firmwareReleases({ showArchived, showDeploymentHistory }),
    enabled: showArchived || showDeploymentHistory,
    refetchInterval: 5_000,
  });
  const lifecycleSettings = useQuery({ queryKey: ['firmware-lifecycle-settings'], queryFn: api.firmwareLifecycleSettings, retry: false });
  const sourceReleases = expandedFirmware.data?.releases ?? releases;
  const lifecycleReconciliation = expandedFirmware.data?.reconciliation ?? reconciliation;
  const displayedReleases = sourceReleases.filter((release) => {
    const state = firmwareReleaseState(release);
    return state !== 'deleted' && (showArchived || state !== 'archived');
  });
  const retentionValue = retentionDraft ?? (lifecycleSettings.data ? lifecycleSettings.data.deployment_retention_days === null ? 'indefinitely' : String(lifecycleSettings.data.deployment_retention_days) : '');
  const savedRetentionValue = lifecycleSettings.data ? lifecycleSettings.data.deployment_retention_days === null ? 'indefinitely' : String(lifecycleSettings.data.deployment_retention_days) : '';
  const refreshFirmware = () => { void queryClient.invalidateQueries({ queryKey: ['firmware-releases'] }); void queryClient.invalidateQueries({ queryKey: ['devices'] }); };
  const upload = useMutation({ mutationFn: ({ file, fields }: { file: File; fields: FirmwareUploadFields }) => api.uploadFirmware(file, fields), onSuccess: () => { closeUpload(); refreshFirmware(); } });
  const deploy = useMutation({ mutationFn: () => api.deployFirmware(deployTarget?.release_id ?? '', selectedDevices, rollout), onSuccess: () => { setDeployTarget(undefined); refreshFirmware(); } });
  const retry = useMutation({ mutationFn: ({ batchId, deviceId }: { batchId: string; deviceId: string }) => api.retryFirmwareBatch(batchId, [deviceId]), onSuccess: refreshFirmware });
  const cancel = useMutation({ mutationFn: () => api.cancelFirmwareBatch(cancelBatchTarget?.id ?? ''), onSuccess: () => { setCancelBatchTarget(undefined); refreshFirmware(); } });
  const archiveRelease = useMutation({ mutationFn: () => api.archiveFirmwareRelease(archiveReleaseTarget?.release_id ?? ''), onSuccess: () => { setArchiveReleaseTarget(undefined); refreshFirmware(); } });
  const restoreRelease = useMutation({ mutationFn: (releaseId: string) => api.restoreFirmwareRelease(releaseId), onSuccess: refreshFirmware });
  const makeCurrent = useMutation({ mutationFn: () => {
    if (!makeCurrentTarget) throw new Error('Choose a firmware release first.');
    return api.makeFirmwareReleaseCurrent(makeCurrentTarget);
  }, onSuccess: () => { setMakeCurrentTarget(undefined); refreshFirmware(); } });
  const updateRollbackProtection = useMutation({ mutationFn: () => {
    if (!rollbackProtectionTarget) throw new Error('Choose a firmware release first.');
    return api.updateFirmwareRollbackProtection(rollbackProtectionTarget.release_id, !rollbackProtectionTarget.rollback_pinned);
  }, onSuccess: () => { setRollbackProtectionTarget(undefined); refreshFirmware(); } });
  const archiveBatch = useMutation({ mutationFn: () => api.archiveFirmwareDeployment(archiveBatchTarget?.id ?? ''), onSuccess: () => { setArchiveBatchTarget(undefined); refreshFirmware(); } });
  const restoreBatch = useMutation({ mutationFn: (batchId: string) => api.restoreFirmwareDeployment(batchId), onSuccess: refreshFirmware });
  const deleteBatch = useMutation({ mutationFn: () => api.deleteFirmwareDeploymentPermanently(deleteBatchTarget?.id ?? ''), onSuccess: () => { setDeleteBatchTarget(undefined); setDeleteBatchConfirmation(''); refreshFirmware(); } });
  const updateRetention = useMutation({
    mutationFn: () => api.updateFirmwareLifecycleSettings(retentionValue === 'indefinitely' ? null : Number(retentionValue) as 90 | 180 | 365),
    onSuccess: () => { setRetentionDraft(undefined); setRetentionConfirmOpen(false); void queryClient.invalidateQueries({ queryKey: ['firmware-lifecycle-settings'] }); },
  });
  const deleteArtifact = useMutation({
    mutationFn: () => {
      if (!deleteArtifactTarget) throw new Error('Choose a firmware release first.');
      return api.deleteFirmwareReleasePermanently(deleteArtifactTarget);
    },
    onSuccess: () => {
      setDeleteArtifactTarget(undefined);
      setDeleteReleaseConfirmation('');
      refreshFirmware();
    },
  });
  function prepareSelectedFiles(image: File | undefined, manifest: File | undefined, notes: File | undefined) {
    const generation = ++preparationGeneration.current;
    setPreparedUpload(undefined);
    setPreparationError(undefined);
    if (!image || !manifest) {
      setPreparingUpload(false);
      return;
    }
    setPreparingUpload(true);
    void prepareFirmwareUpload(image, manifest, notes)
      .then((prepared) => { if (generation === preparationGeneration.current) setPreparedUpload(prepared); })
      .catch((reason: unknown) => { if (generation === preparationGeneration.current) setPreparationError(reason instanceof Error ? reason.message : 'Firmware release files could not be verified.'); })
      .finally(() => { if (generation === preparationGeneration.current) setPreparingUpload(false); });
  }
  function closeUpload() { preparationGeneration.current += 1; setUploadOpen(false); setFirmwareImage(undefined); setFirmwareManifest(undefined); setFirmwareNotes(undefined); setPreparedUpload(undefined); setPreparationError(undefined); setPreparingUpload(false); upload.reset(); }
  function submitUpload(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!firmwareImage || !preparedUpload) return; upload.mutate({ file: firmwareImage, fields: preparedUpload.fields }); }
  function prepareDeploy(release: FirmwareRelease) { const eligible = devices.filter((device) => firmwareUpgradeAvailable(device.firmware_version, release.semantic_version)); setDeployTarget(release); setSelectedDevices(eligible[0] ? [eligible[0].id] : []); setRollout('staged'); deploy.reset(); }
  if (loading) return <Card title="Firmware"><Loading /></Card>;
  if (error) return <ErrorState error={error} />;
  return <><Card title="Firmware" eyebrow="Signed releases and confirmed sensor updates" action={<div className="card-actions"><button type="button" className="button button-secondary" aria-pressed={showArchived} onClick={() => setShowArchived((current) => !current)}>{showArchived ? 'Hide archived' : 'Show archived'}</button><button type="button" className="button button-secondary" aria-pressed={showDeploymentHistory} onClick={() => setShowDeploymentHistory((current) => !current)}>{showDeploymentHistory ? 'Hide deployment history' : 'Show deployment history'}</button>{can('firmware.manage') && <button type="button" className="button button-primary" onClick={() => setUploadOpen(true)}><UploadCloud aria-hidden="true" /> Upload release</button>}</div>}>
    {expandedFirmware.isLoading && <Loading label="Loading firmware archive" />}
    {expandedFirmware.isError && <ErrorState error={expandedFirmware.error} retry={() => void expandedFirmware.refetch()} />}
    {lifecycleReconciliation && lifecycleReconciliation.inconsistent_release_ids.length > 0 && <Notice kind="warning">{lifecycleReconciliation.inconsistent_release_ids.length} firmware lifecycle record{lifecycleReconciliation.inconsistent_release_ids.length === 1 ? '' : 's'} need administrator attention. No record was silently deleted.</Notice>}
    {!expandedFirmware.isLoading && displayedReleases.length === 0 ? <EmptyState title={showArchived ? 'No archived firmware releases' : 'No current firmware releases'} detail={showArchived ? 'No archived release is available to restore.' : 'No signed server-side release manifest is available. Sensors remain on their installed firmware.'} /> : <div className="release-list">{displayedReleases.map((release) => <FirmwareReleaseRow key={release.release_id} release={release} devices={devices} canManage={can('firmware.manage')} retryPending={retry.isPending} onDeploy={() => prepareDeploy(release)} onMakeCurrent={() => { makeCurrent.reset(); setMakeCurrentTarget(release); }} onToggleRollback={() => { updateRollbackProtection.reset(); setRollbackProtectionTarget(release); }} onArchive={() => { archiveRelease.reset(); setArchiveReleaseTarget(release); }} onRestore={() => restoreRelease.mutate(release.release_id)} onDelete={() => { deleteArtifact.reset(); setDeleteReleaseConfirmation(''); setDeleteArtifactTarget(release); }} onRetry={(batchId, deviceId) => retry.mutate({ batchId, deviceId })} onCancel={(batch) => { cancel.reset(); setCancelBatchTarget(batch); }} onArchiveBatch={(batch) => { archiveBatch.reset(); setArchiveBatchTarget(batch); }} onRestoreBatch={(batch) => restoreBatch.mutate(batch.id)} onDeleteBatch={(batch) => { deleteBatch.reset(); setDeleteBatchConfirmation(''); setDeleteBatchTarget(batch); }} />)}</div>}
    {retry.isError && <Notice kind="warning">{retry.error instanceof Error ? retry.error.message : 'The selected sensor could not be retried.'}</Notice>}
    {restoreRelease.isError && <Notice kind="warning">{restoreRelease.error instanceof Error ? restoreRelease.error.message : 'The archived release could not be restored.'}</Notice>}
    {restoreBatch.isError && <Notice kind="warning">{restoreBatch.error instanceof Error ? restoreBatch.error.message : 'The archived deployment could not be restored.'}</Notice>}
    {makeCurrent.isError && <Notice kind="warning">{makeCurrent.error instanceof Error ? makeCurrent.error.message : 'The current firmware release could not be changed.'}</Notice>}
    {updateRollbackProtection.isError && <Notice kind="warning">{updateRollbackProtection.error instanceof Error ? updateRollbackProtection.error.message : 'Rollback protection could not be changed.'}</Notice>}
    <Notice>Hardware certification remains separate from release validation. An update is complete only after the targeted sensor restarts and reports the expected version and build. Archived releases are hidden by default and cannot be deployed.</Notice>
    {lifecycleReconciliation && <details className="technical-details"><summary>Lifecycle reconciliation report</summary><dl><div><dt>Strategy</dt><dd>{lifecycleReconciliation.strategy.replaceAll('_', ' ')}</dd></div><div><dt>Records needing attention</dt><dd>{lifecycleReconciliation.inconsistent_release_ids.length > 0 ? lifecycleReconciliation.inconsistent_release_ids.join(', ') : 'None'}</dd></div><div><dt>Silent deletion performed</dt><dd>{lifecycleReconciliation.silent_deletion_performed ? 'Yes' : 'No'}</dd></div></dl></details>}
    <section className="firmware-retention" aria-labelledby="firmware-retention-heading"><div><h3 id="firmware-retention-heading">Deployment history retention</h3><p>Cleanup applies only to archived, terminal deployment details. Firmware files and active updates are never removed by this setting.</p></div>{lifecycleSettings.isLoading ? <Loading label="Loading deployment retention" /> : lifecycleSettings.isError ? <Notice>Deployment retention controls are not available from this server version.</Notice> : <div className="firmware-retention-controls"><div className="field"><label htmlFor="firmware-retention-days">Keep archived deployment details</label><select id="firmware-retention-days" value={retentionValue} onChange={(event) => setRetentionDraft(event.target.value)}><option value="90">90 days</option><option value="180">180 days</option><option value="365">1 year</option><option value="indefinitely">Indefinitely</option></select></div>{can('firmware.manage') && <button type="button" className="button button-secondary" disabled={!retentionValue || retentionValue === savedRetentionValue} onClick={() => setRetentionConfirmOpen(true)}>Review retention change</button>}</div>}</section>
    <div className="release-list">{devices.map((device) => <article key={device.id}><Wifi aria-hidden="true" /><div><strong>{device.friendly_name}</strong><span>Installed {device.firmware_version ?? 'version unavailable'} · {device.last_command?.type === 'ota_install' ? `${device.last_command.state} ${device.last_command.progress_percent}%${device.last_command.result_code ? ` · ${device.last_command.result_code}` : ''}` : 'no OTA command in progress'}</span></div><StatusPill state={device.last_command?.type === 'ota_install' ? device.last_command.state : 'neutral'} /></article>)}</div>
  </Card>
  <Dialog open={uploadOpen} title="Upload a firmware release" description="Select firmware.bin and manifest.json from the same official release. Version, build, board, compatibility and SHA-256 are filled and verified automatically before upload." onClose={closeUpload}><form className="settings-form" onSubmit={submitUpload}><div className="field"><label htmlFor="firmware-image">Firmware binary</label><input id="firmware-image" type="file" accept="application/octet-stream,.bin" required onChange={(event) => { const file = event.target.files?.[0]; setFirmwareImage(file); prepareSelectedFiles(file, firmwareManifest, firmwareNotes); }} /></div><div className="field"><label htmlFor="firmware-manifest">Release manifest</label><input id="firmware-manifest" type="file" accept="application/json,.json" required onChange={(event) => { const file = event.target.files?.[0]; setFirmwareManifest(file); prepareSelectedFiles(firmwareImage, file, firmwareNotes); }} /><small>Use manifest.json from the same GitHub firmware release. The browser verifies the exact image size and SHA-256; the server verifies them again.</small></div><div className="field"><label htmlFor="firmware-notes-file">Release notes (optional)</label><input id="firmware-notes-file" type="file" accept="text/markdown,text/plain,.md,.txt" onChange={(event) => { const file = event.target.files?.[0]; setFirmwareNotes(file); prepareSelectedFiles(firmwareImage, firmwareManifest, file); }} /><small>If omitted, release notes are generated from the verified manifest identity.</small></div>{preparingUpload && <Notice>Reading and verifying the selected release files…</Notice>}{preparationError && <Notice kind="warning">{preparationError}</Notice>}{preparedUpload && <><Notice kind="success">Release files match. All OTA metadata has been filled automatically.</Notice><dl><div><dt>Version / build</dt><dd>{preparedUpload.fields.semantic_version} · {preparedUpload.fields.build_number}</dd></div><div><dt>Target</dt><dd>{preparedUpload.projectName} · {preparedUpload.targetChip}</dd></div><div><dt>Board profile</dt><dd>{preparedUpload.fields.board_profile}</dd></div><div><dt>Minimum boot / config</dt><dd>{preparedUpload.fields.minimum_boot_version} / {preparedUpload.fields.minimum_config_version}</dd></div><div><dt>Image</dt><dd>{bytes(preparedUpload.imageSize)} · SHA-256 {preparedUpload.fields.expected_sha256.slice(0, 16)}…</dd></div><div><dt>Hardware certification</dt><dd>{preparedUpload.hardwareCertification}</dd></div></dl></>}{upload.isError && <Notice kind="warning">{upload.error instanceof Error ? upload.error.message : 'Firmware upload failed.'}</Notice>}<div className="dialog-actions"><button type="button" className="button button-secondary" onClick={closeUpload}>Cancel</button><button type="submit" className="button button-primary" disabled={upload.isPending || preparingUpload || !preparedUpload}>{upload.isPending ? 'Uploading…' : 'Upload verified candidate'}</button></div></form></Dialog>
  <ConfirmDialog open={Boolean(deployTarget)} title={`Deploy firmware ${deployTarget?.semantic_version ?? ''}?`} description={<div><p>Choose authenticated target sensors and rollout mode. A staged rollout queues the first target and holds later targets; an immediate rollout queues every selected target.</p><div className="permission-grid">{devices.map((device) => { const eligible = !deployTarget || firmwareUpgradeAvailable(device.firmware_version, deployTarget.semantic_version); return <label key={device.id}><input type="checkbox" checked={selectedDevices.includes(device.id)} disabled={!eligible} onChange={(event) => setSelectedDevices((current) => event.target.checked ? [...current, device.id] : current.filter((id) => id !== device.id))} /><span><strong>{device.friendly_name}</strong><small>{eligible ? `Installed ${device.firmware_version ?? 'unknown'}` : `Already on ${device.firmware_version}; OTA accepts upgrades only`}</small></span></label>; })}</div><div className="appearance-options"><label><input type="radio" name="rollout" checked={rollout === 'staged'} onChange={() => setRollout('staged')} /><span><strong>Staged</strong><small>Queue one target first</small></span></label><label><input type="radio" name="rollout" checked={rollout === 'immediate'} onChange={() => setRollout('immediate')} /><span><strong>Immediate</strong><small>Queue all selected targets</small></span></label></div>{deployTarget && devices.every((device) => !firmwareUpgradeAvailable(device.firmware_version, deployTarget.semantic_version)) && <Notice>Every selected-home sensor already has this version or a newer version. The firmware intentionally rejects same-version and downgrade OTA attempts.</Notice>}{deploy.isError && <Notice kind="warning">{deploy.error instanceof Error ? deploy.error.message : 'Deployment failed.'}</Notice>}</div>} confirmLabel="Queue deployment" busy={deploy.isPending} confirmDisabled={selectedDevices.length === 0} onCancel={() => setDeployTarget(undefined)} onConfirm={() => { if (selectedDevices.length > 0) deploy.mutate(); }} tone="warning" />
  <ConfirmDialog open={Boolean(archiveReleaseTarget)} title={`Archive firmware ${archiveReleaseTarget?.semantic_version ?? ''}?`} description={<div><p>This release will disappear from the default list and cannot start new deployments. Its artifact, deployment history, and audit record remain available.</p>{archiveRelease.isError && <Notice kind="warning">{archiveRelease.error instanceof Error ? archiveRelease.error.message : 'The release could not be archived.'}</Notice>}</div>} confirmLabel="Archive release" busy={archiveRelease.isPending} onCancel={() => setArchiveReleaseTarget(undefined)} onConfirm={() => archiveRelease.mutate()} tone="warning" />
  <ConfirmDialog open={Boolean(makeCurrentTarget)} title={`Make firmware ${makeCurrentTarget?.semantic_version ?? ''} current?`} description={<div><p>This selects the release for new firmware updates. The server verifies its exact version, digest, available artifact, and lifecycle state. The previous current release is retained and protected for rollback.</p></div>} confirmLabel="Make current firmware" busy={makeCurrent.isPending} onCancel={() => setMakeCurrentTarget(undefined)} onConfirm={() => makeCurrent.mutate()} tone="warning" />
  <ConfirmDialog open={Boolean(rollbackProtectionTarget)} title={`${rollbackProtectionTarget?.rollback_pinned ? 'Remove rollback protection from' : 'Protect'} firmware ${rollbackProtectionTarget?.semantic_version ?? ''}?`} description={<div><p>{rollbackProtectionTarget?.rollback_pinned ? 'Removing this protection may make the release eligible for archival or permanent deletion when no other safety rule protects it.' : 'Rollback protection prevents permanent deletion while this artifact may be needed for recovery.'}</p></div>} confirmLabel={rollbackProtectionTarget?.rollback_pinned ? 'Remove rollback protection' : 'Protect for rollback'} busy={updateRollbackProtection.isPending} onCancel={() => setRollbackProtectionTarget(undefined)} onConfirm={() => updateRollbackProtection.mutate()} tone="warning" />
  <ConfirmDialog open={Boolean(deleteArtifactTarget)} title={`Delete firmware ${deleteArtifactTarget?.semantic_version ?? ''} permanently?`} description={<div><p>This action is irreversible. The server will recheck every deployment, sensor report, rollback pin, and artifact reference inside the deletion transaction.</p>{deleteArtifactTarget && <dl><div><dt>Version / build</dt><dd>{deleteArtifactTarget.semantic_version} · {deleteArtifactTarget.build_number}</dd></div><div><dt>Digest</dt><dd>{deleteArtifactTarget.sha256.slice(0, 16)}…</dd></div><div><dt>Deployment records</dt><dd>{deleteArtifactTarget.deployment_count ?? deleteArtifactTarget.deployment_batches.length}</dd></div><div><dt>Sensors reporting version</dt><dd>{deleteArtifactTarget.sensor_reported_count ?? 'Not reported'}</dd></div><div><dt>Audit information retained</dt><dd>Version, build, digest, deletion actor, time, and result</dd></div></dl>}<div className="field"><label htmlFor="delete-release-confirmation">Type DELETE RELEASE PERMANENTLY</label><input id="delete-release-confirmation" value={deleteReleaseConfirmation} onChange={(event) => setDeleteReleaseConfirmation(event.target.value)} autoComplete="off" /></div>{deleteArtifact.isError && <Notice kind="warning">{deleteArtifact.error instanceof Error ? deleteArtifact.error.message : 'The firmware release could not be deleted.'}</Notice>}</div>} confirmLabel="Delete release permanently" busy={deleteArtifact.isPending} confirmDisabled={deleteReleaseConfirmation !== 'DELETE RELEASE PERMANENTLY'} onCancel={() => { setDeleteArtifactTarget(undefined); setDeleteReleaseConfirmation(''); }} onConfirm={() => deleteArtifact.mutate()} tone="danger" />
  <ConfirmDialog open={Boolean(archiveBatchTarget)} title="Archive this completed deployment?" description={<div><p>The deployment will move to deployment history. Final sensor results, security evidence, audit records, and its firmware release remain intact.</p>{archiveBatch.isError && <Notice kind="warning">{archiveBatch.error instanceof Error ? archiveBatch.error.message : 'The deployment could not be archived.'}</Notice>}</div>} confirmLabel="Archive deployment" busy={archiveBatch.isPending} onCancel={() => setArchiveBatchTarget(undefined)} onConfirm={() => archiveBatch.mutate()} tone="warning" />
  <ConfirmDialog open={Boolean(deleteBatchTarget)} title="Delete this deployment record?" description={<div><p>Only archived, terminal deployment details without a troubleshooting hold can be deleted. A compact audit event remains, and the firmware artifact is not removed.</p>{deleteBatchTarget && <div className="field"><label htmlFor="delete-deployment-confirmation">Type deployment ID {deleteBatchTarget.id}</label><input id="delete-deployment-confirmation" value={deleteBatchConfirmation} onChange={(event) => setDeleteBatchConfirmation(event.target.value)} autoComplete="off" /></div>}{deleteBatch.isError && <Notice kind="warning">{deleteBatch.error instanceof Error ? deleteBatch.error.message : 'The deployment record could not be deleted.'}</Notice>}</div>} confirmLabel="Delete deployment record" busy={deleteBatch.isPending} confirmDisabled={!deleteBatchTarget || deleteBatchConfirmation !== deleteBatchTarget.id} onCancel={() => { setDeleteBatchTarget(undefined); setDeleteBatchConfirmation(''); }} onConfirm={() => deleteBatch.mutate()} tone="danger" />
  <ConfirmDialog open={retentionConfirmOpen} title="Change deployment history retention?" description={<div><p>{retentionValue === 'indefinitely' ? 'Archived deployment details will be kept indefinitely.' : `Archived terminal deployment details older than ${retentionValue} days may be reduced to audit tombstones.`}</p><p>Firmware artifacts and active deployments are not affected.</p>{updateRetention.isError && <Notice kind="warning">{updateRetention.error instanceof Error ? updateRetention.error.message : 'Retention could not be updated.'}</Notice>}</div>} confirmLabel="Save retention policy" busy={updateRetention.isPending} onCancel={() => setRetentionConfirmOpen(false)} onConfirm={() => updateRetention.mutate()} tone="warning" />
  <ConfirmDialog open={Boolean(cancelBatchTarget)} title="Cancel waiting firmware jobs?" description={<div><p>Only jobs that have not been delivered will be canceled. An update already downloading, installing, restarting, or confirming cannot be reversed safely.</p>{cancel.isError && <Notice kind="warning">{cancel.error instanceof Error ? cancel.error.message : 'The deployment could not be canceled.'}</Notice>}</div>} confirmLabel="Cancel waiting jobs" busy={cancel.isPending} onCancel={() => setCancelBatchTarget(undefined)} onConfirm={() => cancel.mutate()} tone="danger" />
  </>;
}

function BackupSettings({ backup, loading, error }: { backup: Awaited<ReturnType<typeof api.backups>> | undefined; loading: boolean; error: unknown }) {
  if (loading) return <Card title="Backups & restore"><Loading /></Card>;
  if (error) return <ErrorState error={error} />;
  const successful = backup?.last_successful_backup;
  const restore = backup?.last_successful_restore_test;
  return <Card title="Backups & restore" eyebrow="Encrypted backup with independent restore evidence"><div className="backup-state"><HardDrive aria-hidden="true" /><div><strong>Backup evidence</strong><StatusPill state={typeof successful?.state === 'string' ? successful.state : 'unavailable'} /></div></div><dl><div><dt>Last successful backup</dt><dd>{typeof successful?.completed_at === 'string' ? dateTime(successful.completed_at) : 'Not available'}</dd></div><div><dt>Backup checksum</dt><dd>{typeof successful?.sha256 === 'string' ? `${successful.sha256.slice(0, 16)}…` : 'Not available'}</dd></div><div><dt>Last isolated restore test</dt><dd>{typeof restore?.completed_at === 'string' ? dateTime(restore.completed_at) : 'Not available'}</dd></div><div><dt>Verification rule</dt><dd>{backup?.verification_rule ?? 'Not available'}</dd></div></dl><p className="disclosure">“Verified” requires checksum, decrypt, pg_restore listing and isolated restore evidence; file existence alone is never reported as verification.</p></Card>;
}

function AppearanceSettings() {
  const [dirty, setDirty] = useState(false);
  const query = useQuery({ queryKey: ['preferences'], queryFn: api.preferences });
  const mutation = useMutation({ mutationFn: (payload: Awaited<ReturnType<typeof api.preferences>>) => api.updatePreferences(payload), onSuccess: (value) => { setDirty(false); document.documentElement.dataset.density = value.density; void query.refetch(); } });
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault(); };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); const form = new FormData(event.currentTarget); mutation.mutate({ dashboard_range: formString(form, 'dashboardRange') as 'today' | 'week' | 'month', history_range: formString(form, 'historyRange') as 'day' | 'week' | 'month' | 'billing_cycle', refresh_seconds: Number(formString(form, 'refreshSeconds')) as 15 | 30 | 60 | 120 | 300, power_unit: formString(form, 'powerUnit') as 'auto' | 'W' | 'kW', energy_unit: formString(form, 'energyUnit') as 'auto' | 'Wh' | 'kWh', date_format: formString(form, 'dateFormat') as 'iso' | 'us', time_format: formString(form, 'timeFormat') as '12h' | '24h', decimal_precision: Number(formString(form, 'decimalPrecision')), density: formString(form, 'density') as 'comfortable' | 'compact', dashboard_cards: form.getAll('dashboardCards').map(String) as Array<'live_power' | 'energy' | 'cost' | 'completeness' | 'alerts'>, ...(query.data?.display_timezone ? { display_timezone: query.data.display_timezone } : {}) }); }
  if (query.isLoading) return <Card title="Display & units"><Loading label="Loading display preferences" /></Card>;
  if (query.isError || !query.data) return <ErrorState error={query.error ?? new Error('Display preferences are unavailable.')} retry={() => void query.refetch()} />;
  const value = query.data;
  return <Card title="Display & units" eyebrow="Per-user, server-persisted preferences"><form key={JSON.stringify(value)} className="settings-form" onSubmit={submit} onChange={() => setDirty(true)} onReset={() => setDirty(false)}><div className="filter-row"><div className="field"><label htmlFor="pref-dashboard-range">Default dashboard range</label><select id="pref-dashboard-range" name="dashboardRange" defaultValue={value.dashboard_range}><option value="today">Today</option><option value="week">Week</option><option value="month">Month</option></select></div><div className="field"><label htmlFor="pref-history-range">Default History range</label><select id="pref-history-range" name="historyRange" defaultValue={value.history_range}><option value="day">Day</option><option value="week">Week</option><option value="month">Month</option><option value="billing_cycle">Billing cycle</option></select></div><div className="field"><label htmlFor="pref-refresh">Refresh interval</label><select id="pref-refresh" name="refreshSeconds" defaultValue={String(value.refresh_seconds)}><option value="15">15 seconds</option><option value="30">30 seconds</option><option value="60">1 minute</option><option value="120">2 minutes</option><option value="300">5 minutes</option></select></div></div><div className="filter-row"><div className="field"><label htmlFor="pref-power-unit">Power unit</label><select id="pref-power-unit" name="powerUnit" defaultValue={value.power_unit}><option value="auto">Automatic W/kW</option><option value="W">Watts</option><option value="kW">Kilowatts</option></select></div><div className="field"><label htmlFor="pref-energy-unit">Energy unit</label><select id="pref-energy-unit" name="energyUnit" defaultValue={value.energy_unit}><option value="auto">Automatic Wh/kWh</option><option value="Wh">Watt-hours</option><option value="kWh">Kilowatt-hours</option></select></div><div className="field"><label htmlFor="pref-decimals">Decimal precision</label><input id="pref-decimals" name="decimalPrecision" type="number" min="0" max="4" defaultValue={value.decimal_precision} required /></div></div><div className="filter-row"><div className="field"><label htmlFor="pref-date-format">Date format</label><select id="pref-date-format" name="dateFormat" defaultValue={value.date_format}><option value="us">Month/day/year</option><option value="iso">Year-month-day</option></select></div><div className="field"><label htmlFor="pref-time-format">Time format</label><select id="pref-time-format" name="timeFormat" defaultValue={value.time_format}><option value="12h">12-hour</option><option value="24h">24-hour</option></select></div></div><fieldset><legend>Information density</legend><div className="appearance-options"><label><input type="radio" name="density" value="comfortable" defaultChecked={value.density === 'comfortable'} /><span><strong>Comfortable</strong><small>Balanced spacing and touch targets</small></span></label><label><input type="radio" name="density" value="compact" defaultChecked={value.density === 'compact'} /><span><strong>Compact</strong><small>Higher density on larger screens</small></span></label></div></fieldset><fieldset><legend>Dashboard cards</legend><div className="permission-grid">{(['live_power', 'energy', 'cost', 'completeness', 'alerts'] as const).map((card) => <label key={card}><input type="checkbox" name="dashboardCards" value={card} defaultChecked={value.dashboard_cards.includes(card)} /><span><strong>{card.replaceAll('_', ' ')}</strong></span></label>)}</div></fieldset>{dirty && <Notice>You have unsaved display preference changes.</Notice>}{mutation.isSuccess && <Notice kind="success">Display preferences were saved to your account.</Notice>}{mutation.isError && <Notice kind="warning">{mutation.error instanceof Error ? mutation.error.message : 'Preferences could not be saved.'}</Notice>}<Notice>System color contrast, visible keyboard focus and reduced-motion preferences are always respected.</Notice><div className="dialog-actions"><button type="reset" className="button button-secondary">Cancel changes</button><button type="submit" className="button button-primary" disabled={mutation.isPending || !dirty}>{mutation.isPending ? 'Saving…' : 'Save display preferences'}</button></div></form></Card>;
}

function DataPrivacySettings() {
  return <><Card title="Data retention & sources" eyebrow="Clear privacy boundaries"><dl><div><dt>Electrical History</dt><dd>Accepted PZEM readings and connection gaps only; a bill never supplies History.</dd></div><div><dt>Bill uploads</dt><dd>Processed in temporary memory for approved rate fields. Original PDF bytes are never retained; extracted working drafts can be deleted.</dd></div><div><dt>Official SCE rate records</dt><dd>Unpublished or rejected review items can be deleted. Published rate versions and their source records remain protected.</dd></div><div><dt>Firmware releases</dt><dd>Eligible old releases can be archived or permanently deleted after every protected deployment and rollback use has ended.</dd></div><div><dt>Account security</dt><dd>Passwords are one-way hashed. Password changes, resets, disablement and deletion revoke active sessions.</dd></div></dl><Notice>Sensor readings are sent directly to the server. History retention is controlled in Sensors settings.</Notice></Card><Card title="Exports & audit records"><div className="dialog-actions"><PermissionGate permission="history.export"><a className="button button-secondary" href="/history">Open History export</a></PermissionGate><PermissionGate permission="logs.view"><a className="button button-secondary" href="/settings?section=logs">Open audit diagnostics</a></PermissionGate><PermissionGate permission="rates.bill_import"><a className="button button-secondary" href="/settings?section=rates">Open rate-only bill import</a></PermissionGate></div></Card></>;
}

function abbreviated(value: string | null | undefined): string {
  if (!value) return 'Not reported';
  return value.length > 20 ? `${value.slice(0, 16)}…` : value;
}

function compactIdentifier(value: string | number | null | undefined, length = 8): string {
  if (value === null || value === undefined || String(value).length === 0) return 'not reported';
  const text = String(value);
  return text.length > length ? text.slice(0, length) : text;
}

function deliveryStatusLabel(status: string | null | undefined): string {
  const normalized = status?.toLowerCase().replaceAll(' ', '_');
  if (normalized && ['rejected', 'invalid', 'authentication_failed', 'bad_signature'].includes(normalized)) return 'Rejected';
  if (normalized && ['delayed', 'stale', 'backlogged'].includes(normalized)) return 'Delayed';
  if (normalized && ['no_recent_samples', 'offline', 'missing'].includes(normalized)) return 'No recent samples';
  if (normalized && ['received', 'receiving', 'accepted', 'healthy', 'ok'].includes(normalized)) return 'Receiving normally';
  return 'Unknown';
}

function sensorTimeTrust(value: boolean | null | undefined): string {
  if (value === true) return 'Trusted';
  if (value === false) return 'Using server time';
  return 'Not enough information';
}

function acceptedSampleWindow(count: number | undefined, seconds: number | undefined): string {
  if (count === undefined || seconds === undefined) return 'Not reported by this server version';
  const duration = seconds % 3600 === 0 ? `${seconds / 3600} hour${seconds === 3600 ? '' : 's'}` : seconds % 60 === 0 ? `${seconds / 60} minutes` : `${seconds} seconds`;
  return `${count} accepted sample${count === 1 ? '' : 's'} in the last ${duration}`;
}

function HealthSettings({ homeId, health, devices, loading, error }: { homeId: string; health: Awaited<ReturnType<typeof api.health>> | undefined; devices: DeviceDetail[]; loading: boolean; error: unknown }) {
  const [copyState, setCopyState] = useState<'copied' | 'failed' | null>(null);
  if (loading) return <Card title="Diagnostics"><Loading /></Card>;
  if (error) return <ErrorState error={error} />;
  if (!health) return <EmptyState title="Health unavailable" detail="The server has not returned a health snapshot." />;
  const overall = health.database === 'reachable' && health.sensors.every((sensor) => sensor.state === 'online') ? 'healthy' : 'degraded';
  async function copyHomeId() {
    if (!navigator.clipboard || !homeId) { setCopyState('failed'); return; }
    try { await navigator.clipboard.writeText(homeId); setCopyState('copied'); } catch { setCopyState('failed'); }
  }
  async function copyValue(value: string) {
    if (!navigator.clipboard) { setCopyState('failed'); return; }
    try { await navigator.clipboard.writeText(value); setCopyState('copied'); } catch { setCopyState('failed'); }
  }
  const protocolCompatible = health.compatibility?.compatible ?? health.protocol === 'pm-protocol/1.0.0';
  return <>
    <Card title="Diagnostics" eyebrow="Build and service status"><div className="health-heading"><Activity aria-hidden="true" /><div><strong>Overall system</strong><span>Checked {dateTime(health.generated_at)}</span></div><StatusPill state={overall} /></div>
      <div className="detail-grid build-identity-grid">
        <section><h3>Frontend</h3><dl><div><dt>Version</dt><dd>{__PM_BUILD__.version}</dd></div><div><dt>Commit</dt><dd>{abbreviated(__PM_BUILD__.revision)}</dd></div><div><dt>Build time</dt><dd>{__PM_BUILD__.buildTime}</dd></div><div><dt>Static asset build</dt><dd>{abbreviated(__PM_BUILD__.assetId)}</dd></div><div><dt>Image digest</dt><dd>{abbreviated(health.frontend?.image_digest)}</dd></div></dl></section>
        <section><h3>Backend</h3><dl><div><dt>Version</dt><dd>{health.backend?.version ?? health.version}</dd></div><div><dt>Commit</dt><dd>{abbreviated(health.backend?.commit)}</dd></div><div><dt>Build time</dt><dd>{health.backend?.build_time ?? 'Not reported'}</dd></div><div><dt>API version</dt><dd>{health.backend?.api_version ?? 'Not reported'}</dd></div><div><dt>Image digest</dt><dd>{abbreviated(health.backend?.image_digest)}</dd></div></dl></section>
        <section><h3>Database</h3><dl><div><dt>Connection</dt><dd>{health.database}</dd></div><div><dt>Current migration</dt><dd>{health.database_migration?.current ?? 'Not reported by this server version'}</dd></div><div><dt>Expected migration</dt><dd>{health.database_migration?.expected ?? 'Not reported by this server version'}</dd></div></dl></section>
        <section><h3>Compatibility</h3><dl><div><dt>Shared protocol</dt><dd>{health.protocol}</dd></div><div><dt>Frontend and backend</dt><dd>{protocolCompatible ? 'Compatible' : 'Incompatible — deploy matching application versions'}</dd></div><div><dt>Database migrations</dt><dd>{health.database_migration?.compatible === false ? 'Required migration is missing' : health.database_migration?.compatible === true ? 'Current' : 'Not reported'}</dd></div></dl></section>
      </div>
      <details className="technical-details"><summary>Technical details</summary><dl><div><dt>Internal home ID</dt><dd><code>{homeId || 'No active home'}</code> <button type="button" className="text-button" onClick={() => void copyHomeId()} disabled={!homeId}>Copy</button>{copyState === 'copied' && <span role="status"> Copied</span>}{copyState === 'failed' && <span role="alert"> Copy unavailable</span>}</dd></div><div><dt>Frontend image</dt><dd>{health.frontend?.image_name ?? 'Not reported'}</dd></div><div><dt>Backend image</dt><dd>{health.backend?.image_name ?? 'Not reported'}</dd></div><div><dt>Cache version</dt><dd>{health.frontend?.cache_version ?? 'No service worker; shell responses must revalidate'}</dd></div></dl></details>
    </Card>
    <Card title="Sensor delivery" eyebrow="Direct server connection">
      <div className="service-grid">{health.sensors.map((sensor) => {
        const detail = devices.find((device) => device.id === sensor.device_id);
        const delivery = sensor.server_delivery_status ?? sensor.synchronization?.server_delivery_status ?? detail?.server_delivery_status ?? detail?.synchronization?.server_delivery_status;
        const receivedAt = sensor.last_server_received_at ?? sensor.synchronization?.last_server_received_at ?? detail?.last_server_received_at ?? detail?.synchronization?.last_server_received_at;
        const measuredAt = sensor.last_sensor_sampled_at ?? sensor.synchronization?.last_sensor_sampled_at ?? detail?.last_sensor_sampled_at ?? detail?.synchronization?.last_sensor_sampled_at;
        const trusted = sensor.sensor_time_trusted ?? sensor.synchronization?.sensor_time_trusted ?? detail?.sensor_time_trusted ?? detail?.synchronization?.sensor_time_trusted;
        const buildId = sensor.firmware_build_id ?? detail?.firmware_build_id;
        const digest = sensor.firmware_digest ?? detail?.firmware_digest;
        const version = sensor.firmware_version ?? detail?.firmware_version ?? 'Not reported';
        return <article key={sensor.device_id}><div><strong>{sensor.device_name ?? detail?.friendly_name ?? `Sensor ${sensor.device_id.slice(0, 8)}`}</strong><StatusPill state={sensor.state} /></div><p>{deliveryStatusLabel(delivery)}</p><dl><div><dt>Last received</dt><dd>{receivedAt ? timeAgo(receivedAt) : 'No authenticated telemetry received'}</dd></div><div><dt>Last measured</dt><dd>{measuredAt ? <>{timeAgo(measuredAt)} <small>(sensor timestamp)</small></> : receivedAt ? <>{timeAgo(receivedAt)} <small>(server received time)</small></> : 'Not enough information'}</dd></div><div><dt>Sensor time</dt><dd>{sensorTimeTrust(trusted)}</dd></div><div><dt>Latest stored History interval</dt><dd>{sensor.latest_stored_history_interval_at ? timeAgo(sensor.latest_stored_history_interval_at) : 'Not reported by this server version'}</dd></div><div><dt>Recent accepted samples</dt><dd>{acceptedSampleWindow(sensor.recent_accepted_sample_count, sensor.recent_acceptance_window_seconds)}</dd></div><div><dt>Firmware</dt><dd><span className="compact-identifier" title={`${version} · build ${buildId ?? 'not reported'}`}>{version} · build {compactIdentifier(buildId)}</span></dd></div></dl><details className="technical-details"><summary>Technical details</summary><dl><div><dt>Sensor ID</dt><dd><code>{sensor.device_id}</code> <button type="button" className="text-button" onClick={() => void copyValue(sensor.device_id)}>Copy</button></dd></div><div><dt>Firmware build ID</dt><dd>{buildId === null || buildId === undefined ? 'Not reported by the installed firmware' : <><code>{String(buildId)}</code> <button type="button" className="text-button" onClick={() => void copyValue(String(buildId))}>Copy</button></>}</dd></div><div><dt>Firmware digest</dt><dd>{digest ? <><code>{digest}</code> <button type="button" className="text-button" onClick={() => void copyValue(digest)}>Copy</button></> : 'Not reported by the installed firmware'}</dd></div><div><dt>Protocol</dt><dd>{sensor.telemetry_protocol ?? detail?.telemetry_protocol ?? sensor.protocol ?? detail?.protocol ?? 'Not reported by the installed firmware'}</dd></div><div><dt>Boot partition</dt><dd>{sensor.boot_partition ?? detail?.boot_partition ?? 'Not reported by the installed firmware'}</dd></div><div><dt>Last successful OTA</dt><dd>{sensor.last_successful_ota ?? detail?.last_successful_ota ?? 'Not reported by the installed firmware'}</dd></div></dl></details></article>;
      })}</div>
    </Card>
  </>;
}

function LogsSettings() {
  const diagnostics = useMutation({ mutationFn: api.exportDiagnostics, onSuccess: (blob) => download(blob, `powermeter-redacted-diagnostics-${new Date().toISOString().slice(0, 10)}.zip`) });
  return <Card title="Logs & diagnostics" eyebrow="Typed events, redaction and checksummed evidence"><div className="diagnostics-panel"><ServerCog aria-hidden="true" /><div><strong>Redacted diagnostics bundle</strong><p>Includes typed event codes, correlation IDs and checksums. Original bill documents, secrets and prohibited customer fields are never retained or included.</p><span>Application log retention is server-configured.</span></div></div><button type="button" className="button button-primary" onClick={() => diagnostics.mutate()} disabled={diagnostics.isPending}><Download aria-hidden="true" /> {diagnostics.isPending ? 'Preparing…' : 'Download redacted bundle'}</button>{diagnostics.isError && <Notice kind="warning">{diagnostics.error instanceof Error ? diagnostics.error.message : 'Diagnostics export failed.'}</Notice>}</Card>;
}
