import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, ArchiveRestore, ChevronRight, Cpu, Download, FileClock, HardDrive, Home, Palette, RefreshCw, ServerCog, Shield, UploadCloud, UserPlus, Users, Wifi } from 'lucide-react';
import { useMemo, useState, type FormEvent, type ReactNode } from 'react';
import { api } from '../api';
import { isForbidden } from '../api/client';
import type { DeviceDetail, FirmwareRelease, User as UserType } from '../api/schemas';
import { PermissionGate } from '../auth/PermissionGate';
import { useSession } from '../auth/SessionContext';
import { SensorDrawer } from '../components/SensorDrawer';
import { Card, ConfirmDialog, Dialog, EmptyState, ErrorState, Loading, Notice, StatusPill } from '../components/ui';
import { formString } from '../lib/form';
import { bytes, dateTime, download, timeAgo } from '../lib/format';

type SectionId = 'home' | 'sensors' | 'users' | 'rates' | 'firmware' | 'backups' | 'appearance' | 'health' | 'logs';
interface SettingsSection { id: SectionId; label: string; icon: ReactNode; permission: string; }
const sections: SettingsSection[] = [
  { id: 'home', label: 'Home & utility', icon: <Home aria-hidden="true" />, permission: 'billing.view' },
  { id: 'sensors', label: 'Sensors', icon: <Wifi aria-hidden="true" />, permission: 'sensors.view' },
  { id: 'users', label: 'Users & access', icon: <Users aria-hidden="true" />, permission: 'users.view' },
  { id: 'rates', label: 'Rates & data sources', icon: <FileClock aria-hidden="true" />, permission: 'rates.view' },
  { id: 'firmware', label: 'Firmware', icon: <Cpu aria-hidden="true" />, permission: 'firmware.view' },
  { id: 'backups', label: 'Backups & restore', icon: <ArchiveRestore aria-hidden="true" />, permission: 'backups.view' },
  { id: 'appearance', label: 'Appearance', icon: <Palette aria-hidden="true" />, permission: 'dashboard.view' },
  { id: 'health', label: 'Advanced system health', icon: <Activity aria-hidden="true" />, permission: 'system.view' },
  { id: 'logs', label: 'Logs & diagnostics', icon: <ServerCog aria-hidden="true" />, permission: 'logs.view' },
];

export function SettingsPage() {
  const { can } = useSession();
  const visible = useMemo(() => sections.filter((section) => can(section.permission)), [can]);
  const [active, setActive] = useState<SectionId>(() => {
    const requested = new URLSearchParams(window.location.search).get('section');
    return sections.some((section) => section.id === requested) ? requested as SectionId : visible[0]?.id ?? 'appearance';
  });
  const devices = useQuery({ queryKey: ['devices'], queryFn: api.devices, enabled: can('sensors.view'), refetchInterval: 30_000 });
  const users = useQuery({ queryKey: ['users'], queryFn: api.users, enabled: can('users.view') });
  const roles = useQuery({ queryKey: ['roles'], queryFn: api.roles, enabled: can('users.view') });
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, enabled: can('system.view'), refetchInterval: 60_000 });
  const backups = useQuery({ queryKey: ['backups'], queryFn: api.backups, enabled: can('backups.view'), refetchInterval: 60_000 });
  const firmware = useQuery({ queryKey: ['firmware-releases'], queryFn: api.firmwareReleases, enabled: can('firmware.view') });
  if (visible.length === 0) return <EmptyState title="No settings available" detail="Your role does not include access to any settings area." />;
  const selected = visible.some((section) => section.id === active) ? active : visible[0]!.id;
  return <div className="page settings-page">
    <header className="page-heading"><div><p className="eyebrow">Permission-scoped configuration</p><h1>Settings</h1><p>Manage your home, sensors and operations without exposing device credentials.</p></div></header>
    <div className="settings-layout"><nav className="settings-nav" aria-label="Settings sections">{visible.map((section) => <button key={section.id} type="button" className={selected === section.id ? 'active' : ''} aria-label={section.label} aria-current={selected === section.id ? 'page' : undefined} onClick={() => setActive(section.id)}>{section.icon}<span>{section.label}</span><ChevronRight aria-hidden="true" /></button>)}</nav><div className="settings-content">
      {selected === 'home' && <HomeSettings />}
      {selected === 'sensors' && <SensorSettings devices={devices.data?.devices ?? []} loading={devices.isLoading} error={devices.error} />}
      {selected === 'users' && <UserSettings users={users.data?.users ?? []} roles={roles.data?.roles ?? []} loading={users.isLoading || roles.isLoading} error={users.error ?? roles.error} />}
      {selected === 'rates' && <RateSettings />}
      {selected === 'firmware' && <FirmwareSettings devices={devices.data?.devices ?? []} releases={firmware.data?.releases ?? []} loading={firmware.isLoading} error={firmware.error} />}
      {selected === 'backups' && <BackupSettings backup={backups.data} loading={backups.isLoading} error={backups.error} />}
      {selected === 'appearance' && <AppearanceSettings />}
      {selected === 'health' && <HealthSettings health={health.data} loading={health.isLoading} error={health.error} />}
      {selected === 'logs' && <LogsSettings />}
    </div></div>
  </div>;
}

function HomeSettings() {
  const { can } = useSession();
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ['home-utility'], queryFn: api.homeUtility });
  const [scopeOverride, setScopeOverride] = useState<string>();
  const update = useMutation({ mutationFn: api.updateHomeUtility, onSuccess: () => { setScopeOverride(undefined); void queryClient.invalidateQueries({ queryKey: ['home-utility'] }); void queryClient.invalidateQueries({ queryKey: ['home'] }); } });
  if (query.isLoading) return <Card title="Home & utility"><Loading /></Card>;
  if (query.isError) return <ErrorState error={query.error} retry={() => void query.refetch()} />;
  if (!query.data) return <EmptyState title="Home settings unavailable" detail="The server returned no home or utility account." />;
  const scope = scopeOverride ?? query.data.utility.cost_scope;
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const baseline = formString(form, 'baselineAllocation');
    const cca = formString(form, 'ccaProvider');
    update.mutate({
      home_name: formString(form, 'homeName'),
      timezone: formString(form, 'timezone'),
      billing_day: Number(formString(form, 'billingDay')),
      cost_scope: formString(form, 'costScope'),
      baseline_allocation_kwh: baseline === '' ? null : baseline,
      cca_provider: cca === '' ? null : cca,
      ...(scope === 'full_account' ? { full_account_confirmation: formString(form, 'fullAccountConfirmation') } : {}),
      ...(scope === 'allocated_account' ? { allocated_account_confirmation: formString(form, 'allocatedAccountConfirmation') } : {}),
    });
  }
  return <Card title="Home & utility" eyebrow="Authoritative schedule, account rules and monitored scope"><form className="settings-form" onSubmit={submit}>
    <div className="filter-row"><div className="field"><label htmlFor="home-setting-name">Home name</label><input id="home-setting-name" name="homeName" defaultValue={query.data.home.name} required maxLength={120} disabled={!can('system.manage')} /></div><div className="field"><label htmlFor="home-setting-timezone">IANA schedule timezone</label><input id="home-setting-timezone" name="timezone" defaultValue={query.data.home.timezone} required maxLength={80} disabled={!can('system.manage')} /></div><div className="field"><label htmlFor="home-setting-billing-day">Billing day</label><input id="home-setting-billing-day" name="billingDay" type="number" min={1} max={28} defaultValue={query.data.utility.billing_day} required disabled={!can('system.manage')} /></div></div>
    <div className="filter-row"><div className="field"><label htmlFor="home-setting-scope">Cost scope</label><select id="home-setting-scope" name="costScope" value={scope} onChange={(event) => setScopeOverride(event.target.value)} disabled={!can('system.manage')}><option value="energy_only">Energy only</option><option value="allocated_account">Allocated account</option><option value="full_account">Full account</option></select></div><div className="field"><label htmlFor="home-setting-baseline">Baseline allocation (kWh)</label><input id="home-setting-baseline" name="baselineAllocation" inputMode="decimal" defaultValue={query.data.utility.baseline_allocation_kwh === null ? '' : String(query.data.utility.baseline_allocation_kwh)} disabled={!can('system.manage')} /></div><div className="field"><label htmlFor="home-setting-cca">CCA provider</label><input id="home-setting-cca" name="ccaProvider" defaultValue={query.data.utility.cca_provider ?? ''} maxLength={120} disabled={!can('system.manage')} /></div></div>
    {scope === 'full_account' && <div className="field"><label htmlFor="full-account-confirmation">Type I UNDERSTAND FULL ACCOUNT SCOPE</label><input id="full-account-confirmation" name="fullAccountConfirmation" required pattern="I UNDERSTAND FULL ACCOUNT SCOPE" autoComplete="off" disabled={!can('system.manage')} /><small>Full-account estimates remain sensor-derived; this confirmation changes only which reviewed cost rules may be allocated.</small></div>}
    {scope === 'allocated_account' && <div className="field"><label htmlFor="allocated-account-confirmation">Type I VERIFIED THIS ALLOCATION SCOPE</label><input id="allocated-account-confirmation" name="allocatedAccountConfirmation" required pattern="I VERIFIED THIS ALLOCATION SCOPE" autoComplete="off" disabled={!can('system.manage')} /><small>Allocated-account pricing is applied only to sensors whose matching allocation scope was explicitly verified.</small></div>}
    <Notice>Usage source: {query.data.usage_source}. Authoritative timestamps remain UTC; SCE schedules evaluate in {query.data.home.timezone}.</Notice>
    {update.isError && <Notice kind="warning">{update.error instanceof Error ? update.error.message : 'Settings could not be saved.'}</Notice>}
    {update.isSuccess && <Notice kind="success">Home and utility settings were saved by the server.</Notice>}
    {can('system.manage') && <button type="submit" className="button button-primary" disabled={update.isPending}>{update.isPending ? 'Saving…' : 'Save home settings'}</button>}
  </form></Card>;
}

function SensorSettings({ devices, loading, error }: { devices: DeviceDetail[]; loading: boolean; error: unknown }) {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<DeviceDetail>();
  const [enrollOpen, setEnrollOpen] = useState(false);
  const [aggregateOpen, setAggregateOpen] = useState(false);
  const [aggregateDevices, setAggregateDevices] = useState<string[]>([]);
  const circuits = useQuery({ queryKey: ['circuits'], queryFn: api.circuits });
  const scopedHomeId = devices[0]?.home_id;
  const scopedDevices = devices.filter((device) => device.home_id === scopedHomeId);
  const enrollment = useMutation({ mutationFn: (payload: { friendlyName: string; ctRating: string }) => api.createEnrollmentToken({ home_id: scopedHomeId ?? '', friendly_name: payload.friendlyName, ct_rating_a: payload.ctRating, pzem_variant: 'pzem004t-v4-classic-candidate', expires_minutes: 15 }) });
  const aggregate = useMutation({
    mutationFn: (payload: { name: string; deviceIds: string[] }) => api.createVerifiedAggregate({ home_id: scopedHomeId ?? '', name: payload.name, device_ids: payload.deviceIds, confirmation: 'I VERIFIED THESE NON-OVERLAPPING METERS' }),
    onSuccess: () => { setAggregateOpen(false); void queryClient.invalidateQueries({ queryKey: ['circuits'] }); void queryClient.invalidateQueries({ queryKey: ['devices'] }); void queryClient.invalidateQueries({ queryKey: ['home'] }); },
  });
  function submitEnrollment(event: FormEvent<HTMLFormElement>) { event.preventDefault(); const form = new FormData(event.currentTarget); enrollment.mutate({ friendlyName: formString(form, 'friendlyName'), ctRating: formString(form, 'ctRating') }); }
  function submitAggregate(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (aggregateDevices.length < 2) return; const form = new FormData(event.currentTarget); aggregate.mutate({ name: formString(form, 'aggregateName'), deviceIds: aggregateDevices }); }
  function closeEnrollment() { enrollment.reset(); setEnrollOpen(false); }
  if (loading) return <Card title="Sensors"><Loading /></Card>;
  if (error) return <ErrorState error={error} />;
  return <><Card title="Sensors" eyebrow="Outbound-only authenticated devices" action={<div className="card-actions"><PermissionGate permission="sensors.configure"><button type="button" className="button button-secondary" onClick={() => { setAggregateDevices([]); setAggregateOpen(true); }} disabled={scopedDevices.length < 2}><Activity aria-hidden="true" /> Verify aggregate</button></PermissionGate><PermissionGate permission="sensors.enroll"><button type="button" className="button button-primary" onClick={() => setEnrollOpen(true)}><Wifi aria-hidden="true" /> Enroll sensor</button></PermissionGate></div>}>{devices.length === 0 ? <EmptyState title="No sensors" detail="Enrollment is unavailable until the server returns a sensor-scoped home identifier; the UI never guesses a home." /> : <div className="settings-list">{devices.map((device) => <button type="button" key={device.id} onClick={() => setSelected(device)}><span className="settings-list-icon"><Wifi aria-hidden="true" /></span><div><strong>{device.friendly_name}</strong><small>{device.device_fingerprint} · heartbeat {timeAgo(device.heartbeat_at)}</small></div><StatusPill state={device.heartbeat_at ? 'online' : 'offline'} /><ChevronRight aria-hidden="true" /></button>)}</div>}{circuits.data && <div className="aggregate-list"><strong>Configured circuit scopes</strong>{circuits.data.circuits.length === 0 ? <small>No deliberate aggregate has been configured.</small> : circuits.data.circuits.map((circuit) => <span key={circuit.id}>{circuit.name} · {circuit.aggregate_mode === 'verified_sum' ? 'verified non-overlapping sum' : circuit.aggregate_mode}</span>)}</div>}</Card><SensorDrawer device={selected} open={Boolean(selected)} onClose={() => setSelected(undefined)} /><Dialog open={enrollOpen} title="Create one-time sensor enrollment" description="The token is short-lived, single-use, and shown only in this browser dialog." onClose={closeEnrollment}>
    {enrollment.data ? <div className="enrollment-token"><Notice kind="warning">Copy this token into the physical USB provisioning workflow now. It is not retrievable after this dialog closes.</Notice><code>{enrollment.data.token}</code><p>Expires {dateTime(enrollment.data.expires_at)}</p><button type="button" className="button button-primary" onClick={closeEnrollment}>I saved the token</button></div> : <form className="settings-form" onSubmit={submitEnrollment}><div className="field"><label htmlFor="enroll-friendly-name">Friendly name</label><input id="enroll-friendly-name" name="friendlyName" required maxLength={120} /></div><div className="field"><label htmlFor="enroll-ct-rating">CT rating (A)</label><input id="enroll-ct-rating" name="ctRating" type="number" min="1" max="1000" step="0.1" defaultValue="100" required /></div><Notice>New one-CT sensors start in energy-only scope. Enrollment never exposes a sensor-side web server.</Notice>{enrollment.isError && <Notice kind="warning">{enrollment.error instanceof Error ? enrollment.error.message : 'Enrollment token creation failed.'}</Notice>}<div className="dialog-actions"><button type="button" className="button button-secondary" onClick={closeEnrollment}>Cancel</button><button type="submit" className="button button-primary" disabled={enrollment.isPending || !scopedHomeId}>{enrollment.isPending ? 'Creating…' : 'Create token'}</button></div></form>}
    {!scopedHomeId && <Notice kind="warning">No sensor-scoped home identifier is available. Enrollment is disabled instead of guessing a home.</Notice>}
  </Dialog><Dialog open={aggregateOpen} title="Create a verified whole-home aggregate" description="Only meters you have physically verified as non-overlapping may be summed. Parent and child meters must never be combined." onClose={() => setAggregateOpen(false)}><form className="settings-form" onSubmit={submitAggregate}><div className="field"><label htmlFor="aggregate-name">Aggregate name</label><input id="aggregate-name" name="aggregateName" required maxLength={120} placeholder="Verified whole home" /></div><fieldset><legend>Select at least two non-overlapping sensors</legend><div className="permission-grid">{scopedDevices.map((device) => <label key={device.id}><input type="checkbox" name="aggregateDevices" value={device.id} checked={aggregateDevices.includes(device.id)} onChange={(event) => setAggregateDevices((current) => event.target.checked ? [...current, device.id] : current.filter((id) => id !== device.id))} /><span><strong>{device.friendly_name}</strong><small>{device.device_fingerprint}</small></span></label>)}</div></fieldset><div className="field"><label htmlFor="aggregate-confirmation">Type I VERIFIED THESE NON-OVERLAPPING METERS</label><input id="aggregate-confirmation" name="confirmation" required pattern="I VERIFIED THESE NON-OVERLAPPING METERS" autoComplete="off" /></div><Notice kind="warning">This opt-in establishes only a sum of authenticated sensor-derived energy and power. Voltage, frequency and power factor are never summed.</Notice>{!scopedHomeId && <Notice kind="warning">No sensor-scoped home identifier is available, so aggregate creation is disabled.</Notice>}{aggregate.isError && <Notice kind="warning">{aggregate.error instanceof Error ? aggregate.error.message : 'Verified aggregate creation failed.'}</Notice>}<div className="dialog-actions"><button type="button" className="button button-secondary" onClick={() => setAggregateOpen(false)}>Cancel</button><button type="submit" className="button button-primary" disabled={aggregate.isPending || !scopedHomeId || aggregateDevices.length < 2}>{aggregate.isPending ? 'Creating…' : 'Create verified aggregate'}</button></div></form></Dialog></>;
}

function UserSettings({ users, roles, loading, error }: { users: UserType[]; roles: Array<{ id: string; name: string; permissions: string[]; built_in: boolean }>; loading: boolean; error: unknown }) {
  const { can } = useSession();
  const [selected, setSelected] = useState<UserType>();
  const [selectedRoles, setSelectedRoles] = useState<string[]>([]);
  const [selectedEnabled, setSelectedEnabled] = useState(true);
  const [addOpen, setAddOpen] = useState(false);
  const queryClient = useQueryClient();
  const update = useMutation({ mutationFn: () => api.updateUser(selected?.id ?? '', { role_names: selectedRoles, enabled: selectedEnabled }), onSuccess: () => { setSelected(undefined); void queryClient.invalidateQueries({ queryKey: ['users'] }); } });
  const create = useMutation({ mutationFn: (payload: { email: string; displayName: string; password: string; roles: string[] }) => api.createUser({ email: payload.email, display_name: payload.displayName, password: payload.password, role_names: payload.roles }), onSuccess: () => { setAddOpen(false); void queryClient.invalidateQueries({ queryKey: ['users'] }); } });
  function submitNewUser(event: FormEvent<HTMLFormElement>) { event.preventDefault(); const form = new FormData(event.currentTarget); create.mutate({ email: formString(form, 'email'), displayName: formString(form, 'displayName'), password: formString(form, 'password'), roles: form.getAll('roles').map(String) }); }
  if (loading) return <Card title="Users & access"><Loading /></Card>;
  if (error) return <ErrorState error={error} />;
  return <><Card title="Users & access" eyebrow="Server-enforced roles and granular permissions" action={can('users.manage') ? <button type="button" className="button button-primary" onClick={() => setAddOpen(true)}><UserPlus aria-hidden="true" /> Add user</button> : undefined}><div className="settings-list">{users.map((user) => <button type="button" key={user.id} onClick={() => { setSelected(user); setSelectedRoles(user.roles); setSelectedEnabled(user.enabled); }}><span className="settings-list-icon"><Shield aria-hidden="true" /></span><div><strong>{user.display_name}</strong><small>{user.email} · {user.roles.join(', ') || 'No role'}</small></div><StatusPill state={user.enabled ? 'online' : 'offline'} label={user.enabled ? 'Enabled' : 'Disabled'} /><ChevronRight aria-hidden="true" /></button>)}</div></Card>
    <Dialog open={Boolean(selected)} title={`Access for ${selected?.display_name ?? ''}`} description="Role permissions are enforced on every server route; hidden controls alone are never authorization." onClose={() => setSelected(undefined)}><label className="account-enabled"><input type="checkbox" checked={selectedEnabled} onChange={(event) => setSelectedEnabled(event.target.checked)} disabled={!can('users.manage')} /><span><strong>Account enabled</strong><small>Disabling revokes the user’s active sessions; the last enabled Owner is protected by the server.</small></span></label><div className="permission-grid">{roles.map((role) => <label key={role.id}><input type="checkbox" checked={selectedRoles.includes(role.name)} disabled={!can('users.manage')} onChange={(event) => setSelectedRoles((current) => event.target.checked ? [...current, role.name] : current.filter((entry) => entry !== role.name))} /><span><strong>{role.name}</strong><small>{role.permissions.length} permissions</small></span></label>)}</div>{update.isError && <Notice kind="warning">{isForbidden(update.error) ? 'The server refused this role change.' : update.error instanceof Error ? update.error.message : 'Role update failed.'}</Notice>}<div className="dialog-actions"><button type="button" className="button button-secondary" onClick={() => setSelected(undefined)}>Cancel</button>{can('users.manage') && <button type="button" className="button button-primary" onClick={() => update.mutate()} disabled={update.isPending || selectedRoles.length === 0}>{update.isPending ? 'Saving…' : 'Save access'}</button>}</div></Dialog>
    <Dialog open={addOpen} title="Add a local user" description="Create a local account and assign one or more server-enforced roles." onClose={() => setAddOpen(false)}><form className="settings-form" onSubmit={submitNewUser}><div className="field"><label htmlFor="new-user-name">Display name</label><input id="new-user-name" name="displayName" required maxLength={120} autoComplete="name" /></div><div className="field"><label htmlFor="new-user-email">Email</label><input id="new-user-email" name="email" type="email" required autoComplete="username" /></div><div className="field"><label htmlFor="new-user-password">Initial password</label><input id="new-user-password" name="password" type="password" required minLength={14} autoComplete="new-password" /></div><fieldset><legend>Roles</legend><div className="permission-grid">{roles.map((role) => <label key={role.id}><input type="checkbox" name="roles" value={role.name} /><span><strong>{role.name}</strong><small>{role.permissions.length} permissions</small></span></label>)}</div></fieldset>{create.isError && <Notice kind="warning">{create.error instanceof Error ? create.error.message : 'User creation failed.'}</Notice>}<div className="dialog-actions"><button type="button" className="button button-secondary" onClick={() => setAddOpen(false)}>Cancel</button><button type="submit" className="button button-primary" disabled={create.isPending}>{create.isPending ? 'Creating…' : 'Create user'}</button></div></form></Dialog>
  </>;
}

function RateSettings() {
  const check = useMutation({ mutationFn: api.checkRates });
  return <Card title="Rates & data sources" eyebrow="Official allowlisted sources and reviewed versions"><div className="settings-callout"><FileClock aria-hidden="true" /><div><h3>Southern California Edison</h3><p>Server-side checks retain immutable source artifacts and create review candidates when verified pricing changes.</p></div><button type="button" className="button button-secondary" onClick={() => check.mutate()} disabled={check.isPending}><RefreshCw className={check.isPending ? 'spin' : ''} aria-hidden="true" /> Check now</button></div><Notice>Utility PDF processing is a rate-source workflow only. It cannot import consumption or create History.</Notice><a className="button button-primary inline-button" href="/billing">Open Billing & rate library</a></Card>;
}

function FirmwareSettings({ devices, releases, loading, error }: { devices: DeviceDetail[]; releases: FirmwareRelease[]; loading: boolean; error: unknown }) {
  const { can } = useSession();
  const queryClient = useQueryClient();
  const [uploadOpen, setUploadOpen] = useState(false);
  const [deployTarget, setDeployTarget] = useState<FirmwareRelease>();
  const [selectedDevices, setSelectedDevices] = useState<string[]>([]);
  const [rollout, setRollout] = useState<'immediate' | 'staged'>('staged');
  const upload = useMutation({ mutationFn: ({ file, fields }: { file: File; fields: { semantic_version: string; build_number: number; board_profile: string; minimum_boot_version: number; minimum_config_version: number; expected_sha256: string; release_notes: string } }) => api.uploadFirmware(file, fields), onSuccess: () => { setUploadOpen(false); void queryClient.invalidateQueries({ queryKey: ['firmware-releases'] }); } });
  const deploy = useMutation({ mutationFn: () => api.deployFirmware(deployTarget?.release_id ?? '', selectedDevices, rollout), onSuccess: () => { setDeployTarget(undefined); void queryClient.invalidateQueries({ queryKey: ['devices'] }); } });
  function submitUpload(event: FormEvent<HTMLFormElement>) { event.preventDefault(); const form = new FormData(event.currentTarget); const input = event.currentTarget.elements.namedItem('firmwareImage'); if (!(input instanceof HTMLInputElement) || !input.files?.[0]) return; upload.mutate({ file: input.files[0], fields: { semantic_version: formString(form, 'semanticVersion'), build_number: Number(formString(form, 'buildNumber')), board_profile: formString(form, 'boardProfile'), minimum_boot_version: Number(formString(form, 'minimumBootVersion')), minimum_config_version: Number(formString(form, 'minimumConfigVersion')), expected_sha256: formString(form, 'expectedSha256'), release_notes: formString(form, 'releaseNotes') } }); }
  function prepareDeploy(release: FirmwareRelease) { setDeployTarget(release); setSelectedDevices(devices[0] ? [devices[0].id] : []); setRollout('staged'); deploy.reset(); }
  if (loading) return <Card title="Firmware"><Loading /></Card>;
  if (error) return <ErrorState error={error} />;
  return <><Card title="Firmware" eyebrow="Signed, compatible, authenticated OTA releases" action={can('firmware.manage') ? <button type="button" className="button button-primary" onClick={() => setUploadOpen(true)}><UploadCloud aria-hidden="true" /> Upload release</button> : undefined}>
    {releases.length === 0 ? <EmptyState title="No firmware releases" detail="No signed server-side release manifest is available. Sensors remain on their installed firmware." /> : <div className="release-list">{releases.map((release) => <article key={release.release_id}><Cpu aria-hidden="true" /><div><strong>{release.semantic_version} · build {release.build_number}</strong><span>{release.project_name} · {release.board_profile} · {bytes(release.image_size)} · SHA-256 {release.sha256.slice(0, 12)}…</span><small>{release.release_notes || 'No release notes'} · physical certification {release.physical_certification ?? 'not reported'}</small></div><StatusPill state={release.candidate ? 'warning' : 'approved'} label={release.candidate ? 'Candidate' : 'Release'} />{can('firmware.manage') && <button type="button" className="button button-secondary" onClick={() => prepareDeploy(release)} disabled={devices.length === 0}>Deploy</button>}</article>)}</div>}
    <Notice>Candidate status never means physical hardware certification is complete. OTA completion is shown only after authenticated deployment, reboot, heartbeat and reading evidence.</Notice>
    <div className="release-list">{devices.map((device) => <article key={device.id}><Wifi aria-hidden="true" /><div><strong>{device.friendly_name}</strong><span>Installed {device.firmware_version ?? 'version unavailable'} · {device.last_command?.type === 'ota_install' ? `${device.last_command.state} ${device.last_command.progress_percent}%` : 'no OTA command in progress'}</span></div><StatusPill state={device.last_command?.type === 'ota_install' ? device.last_command.state : 'neutral'} /></article>)}</div>
  </Card>
  <Dialog open={uploadOpen} title="Upload a firmware release" description="The server verifies size, semantic version, exact SHA-256, board profile and immutable manifest metadata before storing the binary." onClose={() => setUploadOpen(false)}><form className="settings-form" onSubmit={submitUpload}><div className="field"><label htmlFor="firmware-image">Firmware binary</label><input id="firmware-image" name="firmwareImage" type="file" accept="application/octet-stream,.bin" required /></div><div className="filter-row"><div className="field"><label htmlFor="firmware-version">Semantic version</label><input id="firmware-version" name="semanticVersion" placeholder="1.2.3" required pattern="[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?" /></div><div className="field"><label htmlFor="firmware-build">Build number</label><input id="firmware-build" name="buildNumber" type="number" min={1} max={4294967295} required /></div><div className="field"><label htmlFor="firmware-board">Board profile</label><input id="firmware-board" name="boardProfile" required maxLength={80} /></div></div><div className="filter-row"><div className="field"><label htmlFor="firmware-boot-version">Minimum boot version</label><input id="firmware-boot-version" name="minimumBootVersion" type="number" min={1} defaultValue={1} required /></div><div className="field"><label htmlFor="firmware-config-version">Minimum config version</label><input id="firmware-config-version" name="minimumConfigVersion" type="number" min={1} defaultValue={1} required /></div></div><div className="field"><label htmlFor="firmware-sha">Expected SHA-256</label><input id="firmware-sha" name="expectedSha256" required pattern="[a-f0-9]{64}" minLength={64} maxLength={64} spellCheck={false} /></div><div className="field"><label htmlFor="firmware-notes">Release notes</label><textarea id="firmware-notes" name="releaseNotes" maxLength={20000} required /></div>{upload.isError && <Notice kind="warning">{upload.error instanceof Error ? upload.error.message : 'Firmware upload failed.'}</Notice>}<div className="dialog-actions"><button type="button" className="button button-secondary" onClick={() => setUploadOpen(false)}>Cancel</button><button type="submit" className="button button-primary" disabled={upload.isPending}>{upload.isPending ? 'Verifying…' : 'Upload candidate'}</button></div></form></Dialog>
  <ConfirmDialog open={Boolean(deployTarget)} title={`Deploy firmware ${deployTarget?.semantic_version ?? ''}?`} description={<div><p>Choose authenticated target sensors and rollout mode. A staged rollout queues the first target and holds later targets; an immediate rollout queues every selected target.</p><div className="permission-grid">{devices.map((device) => <label key={device.id}><input type="checkbox" checked={selectedDevices.includes(device.id)} onChange={(event) => setSelectedDevices((current) => event.target.checked ? [...current, device.id] : current.filter((id) => id !== device.id))} /><span><strong>{device.friendly_name}</strong><small>Installed {device.firmware_version ?? 'unknown'}</small></span></label>)}</div><div className="appearance-options"><label><input type="radio" name="rollout" checked={rollout === 'staged'} onChange={() => setRollout('staged')} /><span><strong>Staged</strong><small>Queue one target first</small></span></label><label><input type="radio" name="rollout" checked={rollout === 'immediate'} onChange={() => setRollout('immediate')} /><span><strong>Immediate</strong><small>Queue all selected targets</small></span></label></div>{deploy.isError && <Notice kind="warning">{deploy.error instanceof Error ? deploy.error.message : 'Deployment failed.'}</Notice>}</div>} confirmLabel="Queue deployment" busy={deploy.isPending} onCancel={() => setDeployTarget(undefined)} onConfirm={() => { if (selectedDevices.length > 0) deploy.mutate(); }} tone="warning" />
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
  const [density, setDensity] = useState(() => document.documentElement.dataset.density ?? 'comfortable');
  function apply(next: string) { setDensity(next); document.documentElement.dataset.density = next; }
  return <Card title="Appearance" eyebrow="Accessible dark energy dashboard"><div className="appearance-options"><label><input type="radio" name="density" checked={density === 'comfortable'} onChange={() => apply('comfortable')} /><span><strong>Comfortable</strong><small>Balanced spacing and touch targets</small></span></label><label><input type="radio" name="density" checked={density === 'compact'} onChange={() => apply('compact')} /><span><strong>Compact</strong><small>Higher density on larger screens</small></span></label></div><Notice>System color contrast, visible keyboard focus and reduced-motion preferences are always respected.</Notice></Card>;
}

function HealthSettings({ health, loading, error }: { health: Awaited<ReturnType<typeof api.health>> | undefined; loading: boolean; error: unknown }) {
  if (loading) return <Card title="Advanced system health"><Loading /></Card>;
  if (error) return <ErrorState error={error} />;
  if (!health) return <EmptyState title="Health unavailable" detail="The server has not returned a health snapshot." />;
  const overall = health.database === 'reachable' && health.sensors.every((sensor) => sensor.state === 'online') ? 'healthy' : 'degraded';
  return <Card title="Advanced system health" eyebrow="Exact evidence from central services"><div className="health-heading"><Activity aria-hidden="true" /><div><strong>Overall system</strong><span>Checked {dateTime(health.generated_at)}</span></div><StatusPill state={overall} /></div><div className="service-grid"><article><div><strong>Database</strong><StatusPill state={health.database === 'reachable' ? 'healthy' : 'unhealthy'} /></div><p>{health.database}</p></article><article><div><strong>Protocol</strong><StatusPill state="healthy" /></div><p>{health.protocol}</p></article>{health.sensors.map((sensor) => <article key={sensor.device_id}><div><strong>Sensor {sensor.device_id.slice(0, 8)}</strong><StatusPill state={sensor.state} /></div><p>PZEM {sensor.pzem_status}; storage {sensor.storage_status}; backlog {sensor.backlog ?? 'unavailable'}.</p></article>)}<article><div><strong>Hardware certification</strong><StatusPill state="warning" /></div><p>{health.physical_hardware_certification}</p></article></div></Card>;
}

function LogsSettings() {
  const diagnostics = useMutation({ mutationFn: api.exportDiagnostics, onSuccess: (blob) => download(blob, `powermeter-redacted-diagnostics-${new Date().toISOString().slice(0, 10)}.zip`) });
  return <Card title="Logs & diagnostics" eyebrow="Typed events, redaction and checksummed evidence"><div className="diagnostics-panel"><ServerCog aria-hidden="true" /><div><strong>Redacted diagnostics bundle</strong><p>Includes typed event codes, correlation IDs and checksums. Secrets, retained bill documents and prohibited customer fields are excluded.</p><span>Application log retention is server-configured.</span></div></div><button type="button" className="button button-primary" onClick={() => diagnostics.mutate()} disabled={diagnostics.isPending}><Download aria-hidden="true" /> {diagnostics.isPending ? 'Preparing…' : 'Download redacted bundle'}</button>{diagnostics.isError && <Notice kind="warning">{diagnostics.error instanceof Error ? diagnostics.error.message : 'Diagnostics export failed.'}</Notice>}</Card>;
}
