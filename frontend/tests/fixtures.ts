export const allPermissions = [
  'dashboard.view', 'history.view', 'history.export', 'billing.view', 'billing.manage',
  'rates.bill_import', 'rates.view', 'rates.manage', 'rates.sync', 'sensors.view',
  'sensors.enroll', 'sensors.configure', 'sensors.command.reboot', 'sensors.command.sleep',
  'sensors.command.storage_test', 'sensors.command.storage_format', 'sensors.command.ota',
  'sensors.command.data_reset', 'firmware.view', 'firmware.manage', 'users.view',
  'users.manage', 'backups.view', 'backups.manage', 'logs.view', 'system.view', 'system.manage',
];

export const session = {
  authenticated: true,
  bootstrap_required: false,
  user: {
    id: 'user-owner', display_name: 'Alex Morgan', email: 'alex@example.test', roles: ['Owner'],
    permissions: allPermissions, mfa_enabled: true, enabled: true,
  },
};

export const home = {
  devices: [{
    id: 'device-main', friendly_name: 'Main Panel Sensor', state: 'live',
    measurement: { voltage_v: 122.6, current_a: 20.2, active_power_w: 2480, frequency_hz: 60.01, power_factor: 0.97, measured_at: '2026-08-13T17:32:10Z', pzem_status: 'ok' },
    heartbeat_at: '2026-08-13T17:32:10Z', last_committed_at: '2026-08-13T17:31:00Z', backlog: 3, storage_status: 'healthy', firmware_version: 'v1.2.3', measurement_scope: 'energy_only', estimated_cost_per_hour: '0.42656',
  }],
  summaries: {
    today: { energy_kwh: 18.74, cost: '3.21', completeness: .99, missing_intervals: 2 },
    week: { energy_kwh: 118.2, cost: '18.67', completeness: .97, missing_intervals: 19 },
    billing_cycle: { energy_kwh: 392.8, cost: '62.48', completeness: .96, missing_intervals: 55 },
    month: { energy_kwh: 401.1, cost: '64.10', completeness: .96 },
  },
  current_rate: { plan_name: 'SCE TOU-D-4-9PM', version_id: 'rate-version-2026-08', effective_start: '2026-08-01T07:00:00Z', period: 'Off-Peak', price_per_kwh: '0.172', period_start_minute: 1320, period_end_minute: 1440, scope: 'energy_only', fixed_charges_included: false, baseline_credit_included: false, cca_or_direct_access: null },
  generated_at: '2026-08-13T17:32:15Z',
  summary_scope: { kind: 'selected_sensor', device_id: 'device-main', aggregate: false },
  disclosure: { usage_source: 'authenticated PZEM-004T sensor intervals only', estimated_not_utility_bill: true },
};

export const device = {
  id: 'device-main', home_id: '00000000-0000-0000-0000-000000000010', circuit_id: null, friendly_name: 'Main Panel Sensor', device_fingerprint: '8a34f119dd31', credential_fingerprint: 'a'.repeat(64), credential_key_version: 1, credential_rotation: null, firmware_version: 'v1.2.3', protocol: 'pm-protocol/1.0.0',
  pzem_variant: 'pzem004t-v4-classic-candidate', ct_rating_a: '100', measurement_scope: 'energy_only', heartbeat_at: '2026-08-13T17:32:10Z', wifi_rssi: -54, ip_address: '192.0.2.24', pzem_status: 'ok', storage_status: 'healthy',
  oldest_sequence: 100, newest_sequence: 2020, acknowledgement: 2017, backlog: 3, free_internal_heap: 208000, largest_internal_block: 98120,
  last_reboot_reason: 'software_update', last_command: { id: 'cmd-old', type: 'sync_now', state: 'succeeded', progress_percent: 100 },
};

export const homeScopes = [{ id: '00000000-0000-0000-0000-000000000010', name: 'Home' }];

export const homeUtility = {
  home: { id: '00000000-0000-0000-0000-000000000010', name: 'Home', timezone: 'America/Los_Angeles' },
  utility: { id: '00000000-0000-0000-0000-000000000020', utility_name: 'Southern California Edison', timezone: 'America/Los_Angeles', billing_day: 12, cost_scope: 'energy_only', baseline_allocation_kwh: null, cca_provider: null },
  usage_source: 'authenticated PZEM-004T sensor intervals only',
};

export const circuits = { circuits: [] };

export const firmwareReleases = {
  releases: [{
    schema: 'pm-ota-manifest/1.0.0', release_id: '00000000-0000-0000-0000-000000000030', semantic_version: '1.2.4', build_number: 851,
    project_name: 'power-monitor-sensor-headless', target_chip: 'esp32s3', board_profile: 'esp32-s3-devkitc-n16r8-reference/1', minimum_boot_version: 1, minimum_protocol: 'pm-protocol/1.0.0', minimum_config_version: 1,
    image_size: 1_048_576, sha256: 'b'.repeat(64), candidate: true, release_notes: 'Candidate release for staged validation.', physical_certification: 'pending',
  }],
};

const historyTimestamps = ['2026-08-13T10:00:00Z', '2026-08-13T12:00:00Z', '2026-08-13T14:00:00Z', '2026-08-13T16:00:00Z', '2026-08-13T18:00:00Z', '2026-08-13T20:00:00Z', '2026-08-13T22:00:00Z'];
export const history = {
  points: historyTimestamps.map((timestamp, index) => ({ timestamp, value: index === 3 ? null : [0, 2.1, 3.4, 0, 4.55, 2.7, 2.48][index], cost: index === 3 ? null : String(index * .11), quality: index === 3 ? .25 : 1 })),
  energy_kwh: 18.74, cost: '3.21', completeness: .93,
  missing_ranges: [{ start: '2026-08-13T15:30:00Z', end: '2026-08-13T16:30:00Z' }],
  resolution_seconds: 7200, timezone: 'UTC', usage_source: 'authenticated PZEM-004T sensor intervals only', scope: { device_ids: ['device-main'], aggregate: false },
};

export const dailyHistory = {
  ...history,
  points: ['2026-08-07', '2026-08-08', '2026-08-09', '2026-08-10', '2026-08-11', '2026-08-12', '2026-08-13'].map((date, index) => ({ timestamp: `${date}T12:00:00Z`, value: [17.1, 20.6, 19.8, 17.4, 20.5, 19.7, 18.74][index], cost: String(2.8 + index * .1), quality: 1 })),
  missing_ranges: [], resolution_seconds: 86400,
};

export const alerts = {
  alerts: [
    { id: 'alert-backlog', type: 'reading_backlog', severity: 'warning', state: 'open', opened_at: '2026-08-13T17:31:50Z', evidence: { backlog: 3 } },
    { id: 'alert-rate', type: 'rate_source_changed', severity: 'info', state: 'acknowledged', opened_at: '2026-08-13T16:00:00Z', evidence: { review_required: true } },
  ],
};

export const billing = {
  accounts: [{ utility_account_id: 'utility-account-1', plan_name: 'SCE TOU-D-4-9PM', rate_version_id: 'rate-version-2026-08', effective_start: '2026-08-01T07:00:00Z', cost_scope: 'energy_only', baseline_credit_included: false, fixed_charges_included: false, cca_or_direct_access: null }],
  usage_source: 'authenticated PZEM-004T sensor intervals only', rate_import_notice: 'PDFs create reviewed reusable rate-plan drafts only.',
};

export const systemHealth = {
  generated_at: '2026-08-13T17:32:00Z', version: '0.1.0-rc.2', protocol: 'pm-protocol/1.0.0', database: 'reachable',
  sensors: [{ device_id: 'device-main', state: 'online', heartbeat_age_seconds: 5, pzem_status: 'ok', storage_status: 'healthy', backlog: 3 }],
  open_alert_count: 1, last_rate_sync: { id: 'rate-run-old', state: 'review_required', event_code: 'RATE_SOURCE_SNAPSHOT_CAPTURED', completed_at: '2026-08-13T16:00:00Z' },
  backup: {}, restore_test: {}, physical_hardware_certification: 'pending',
};

export const backupStatus = {
  last_successful_backup: { state: 'verified', completed_at: '2026-08-13T09:01:00Z', sha256: 'c'.repeat(64) },
  last_backup_attempt: { state: 'verified', completed_at: '2026-08-13T09:01:00Z' },
  last_successful_restore_test: { state: 'verified', completed_at: '2026-08-12T09:12:00Z' },
  last_restore_test_attempt: { state: 'verified', completed_at: '2026-08-12T09:12:00Z' },
  verification_rule: 'success requires checksum, decrypt, pg_restore listing, and isolated restore evidence',
};

export const settings = {
  home: { name: 'Home', timezone: 'America/Los_Angeles', utility_name: 'Southern California Edison', cost_scope: 'energy_only' },
  users: [{ ...session.user, enabled: true }],
  roles: [{ id: 'role-owner', name: 'Owner', permissions: allPermissions, built_in: true }],
  firmware_releases: [{ id: 'release-124', version: 'v1.2.4', build: '851', state: 'approved', sha256: 'b'.repeat(64) }],
  backup: { last_success_at: '2026-08-13T09:00:00Z', last_verified_at: '2026-08-13T09:01:00Z', last_restore_test_at: '2026-08-12T09:12:00Z', state: 'healthy', encrypted: true },
  health: { state: 'healthy', checked_at: '2026-08-13T17:32:00Z', services: [{ name: 'api', state: 'healthy', detail: 'Database transaction probe passed.' }, { name: 'worker', state: 'healthy', detail: 'Lease and queue probes passed.' }, { name: 'postgres', state: 'healthy', detail: 'Primary storage reachable.' }] },
  logs: { retention_days: 90, last_export_at: null },
};

export function apiResponse(path: string, method = 'GET'): { status: number; body?: unknown; contentType?: string } {
  if (path.endsWith('/auth/bootstrap/status')) return { status: 200, body: { required: false } };
  if (path.endsWith('/auth/me')) return { status: 200, body: session.user };
  if (path.endsWith('/auth/login') || path.endsWith('/auth/bootstrap')) return { status: 200, body: { user: session.user } };
  if (path.endsWith('/settings/home-utility')) return { status: 200, body: homeUtility };
  if (path.endsWith('/home')) return { status: 200, body: home };
  if (path.includes('/history/export')) return { status: 200, body: 'timestamp,value\n2026-08-13T10:00:00Z,0\n', contentType: 'text/csv' };
  if (path.includes('/history')) return { status: 200, body: path.includes('resolution_seconds=86400') ? dailyHistory : history };
  if (path.includes('/devices') && path.endsWith('/commands') && method === 'POST') return { status: 202, body: { command: { id: 'cmd-new', type: 'sync_now', state: 'queued' }, confirmation_token: null } };
  if (path.includes('/devices/') && path.endsWith('/credentials/rotate') && method === 'POST') return { status: 202, body: { rotation: { rotation_id: '00000000-0000-0000-0000-000000000050', credential_fingerprint: 'b'.repeat(64), state: 'pending', overlap_expires_at: '2026-08-13T17:42:10Z', prepare_command_id: '00000000-0000-0000-0000-000000000051', commit_command_id: null, cancel_command_id: null } } };
  if (path.includes('/credentials/rotations/') && path.endsWith('/cancel') && method === 'POST') return { status: 202, body: { rotation: { rotation_id: '00000000-0000-0000-0000-000000000050', credential_fingerprint: 'b'.repeat(64), state: 'pending', overlap_expires_at: '2026-08-13T17:42:10Z', prepare_command_id: '00000000-0000-0000-0000-000000000051', commit_command_id: null, cancel_command_id: '00000000-0000-0000-0000-000000000052' } } };
  if (path.endsWith('/enrollment-tokens') && method === 'POST') return { status: 201, body: { token: 'single-use-enrollment-token-value-000000000000', expires_at: '2026-08-13T17:47:00Z' } };
  if (path.endsWith('/circuits/verified-aggregates') && method === 'POST') return { status: 201, body: { id: '00000000-0000-0000-0000-000000000040', name: 'Verified whole home', device_ids: ['device-main', 'device-secondary'] } };
  if (path.endsWith('/circuits')) return { status: 200, body: circuits };
  if (path.endsWith('/devices')) return { status: 200, body: { home_scopes: homeScopes, devices: [device] } };
  if (path.includes('/devices/') && path.endsWith('/revoke') && method === 'POST') return { status: 204 };
  if (path.includes('/devices/') && method === 'PATCH') return { status: 200, body: { id: device.id, friendly_name: device.friendly_name, measurement_scope: 'energy_only' } };
  if (path.endsWith('/alerts')) return { status: 200, body: alerts };
  if (path.endsWith('/acknowledge')) return { status: 200, body: { id: 'alert-backlog', state: 'acknowledged' } };
  if (path.endsWith('/silence')) return { status: 200, body: { id: 'alert-backlog', silenced_until: '2026-08-14T17:32:00Z' } };
  if (path.endsWith('/billing')) return { status: 200, body: billing };
  if (path.endsWith('/bill-rate-imports')) return { status: 200, body: { extractions: [] } };
  if (path.endsWith('/rate-sources/check-now')) return { status: 202, body: { run_id: 'rate-run-1', state: 'review_required' } };
  if (path.endsWith('/users') && method === 'POST') return { status: 201, body: { id: 'user-new', email: 'new@example.test', display_name: 'New User' } };
  if (path.endsWith('/users')) return { status: 200, body: { users: [session.user] } };
  if (path.endsWith('/roles')) return { status: 200, body: { roles: [{ id: 'role-owner', name: 'Owner', permissions: allPermissions, built_in: true }], available_permissions: allPermissions } };
  if (path.includes('/users/') && method === 'PATCH') return { status: 200, body: { id: session.user.id, enabled: true, display_name: session.user.display_name } };
  if (path.endsWith('/firmware/releases') && method === 'POST') return { status: 201, body: { release: firmwareReleases.releases[0], manifest_signature: 'fixture-signature', physical_certification: 'pending' } };
  if (path.endsWith('/firmware/releases')) return { status: 200, body: firmwareReleases };
  if (path.includes('/firmware/releases/') && path.endsWith('/deploy')) return { status: 202, body: { deployments: [{ id: 'deployment-1', device_id: device.id, state: 'queued' }] } };
  if (path.endsWith('/system/health')) return { status: 200, body: systemHealth };
  if (path.endsWith('/backups/status')) return { status: 200, body: backupStatus };
  if (path.endsWith('/diagnostics/bundle')) return { status: 200, body: 'redacted', contentType: 'application/zip' };
  if (path.endsWith('/auth/logout')) return { status: 204 };
  return { status: 404, body: { type: 'about:blank', title: 'Not found', status: 404, detail: path } };
}
