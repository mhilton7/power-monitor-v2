import { z } from 'zod';
import { ApiError, apiRequest, jsonBody } from './client';
import {
  alertsSchema,
  backupStatusSchema,
  billingSchema,
  circuitSchema,
  commandSchema,
  deviceRotationSchema,
  devicesSchema,
  firmwareReleaseSchema,
  historySchema,
  homeUtilitySchema,
  homeSchema,
  rateDraftSchema,
  roleSchema,
  sessionSchema,
  systemHealthSchema,
  userSchema,
} from './schemas';

function idempotencyKey(): string {
  return crypto.randomUUID();
}

const authUserSchema = z.object({ user: userSchema.pick({ id: true, email: true, display_name: true }).passthrough() });

export const api = {
  session: async () => {
    const bootstrap = await apiRequest('/auth/bootstrap/status', z.object({ required: z.boolean() }));
    if (bootstrap.required) return sessionSchema.parse({ authenticated: false, bootstrap_required: true, user: null });
    try {
      const user = await apiRequest('/auth/me', userSchema);
      return sessionSchema.parse({ authenticated: true, bootstrap_required: false, user });
    } catch (error) {
      if (error instanceof Error && 'status' in error && error.status === 401) return sessionSchema.parse({ authenticated: false, bootstrap_required: false, user: null });
      throw error;
    }
  },
  login: async (email: string, password: string, totp?: string) => {
    await apiRequest('/auth/login', authUserSchema, { method: 'POST', body: jsonBody({ email, password, ...(totp ? { totp_code: totp } : {}) }) });
    const user = await apiRequest('/auth/me', userSchema);
    return sessionSchema.parse({ authenticated: true, bootstrap_required: false, user });
  },
  bootstrap: async (displayName: string, email: string, password: string, homeName = 'Home', timezone = 'America/Los_Angeles') => {
    await apiRequest('/auth/bootstrap', authUserSchema, { method: 'POST', body: jsonBody({ display_name: displayName, email, password, home_name: homeName, timezone }) });
    const user = await apiRequest('/auth/me', userSchema);
    return sessionSchema.parse({ authenticated: true, bootstrap_required: false, user });
  },
  logout: () => apiRequest('/auth/logout', z.undefined(), { method: 'POST' }),
  home: () => apiRequest('/home', homeSchema),
  history: (query: URLSearchParams) => apiRequest(`/history?${query.toString()}`, historySchema),
  exportHistory: async (query: URLSearchParams) => {
    const exportQuery = new URLSearchParams();
    const from = query.get('from'); const to = query.get('to');
    if (from) exportQuery.set('from', from); if (to) exportQuery.set('to', to);
    const response = await fetch(`/api/v1/history/export.csv?${exportQuery.toString()}`, { credentials: 'same-origin', headers: { Accept: 'text/csv' } });
    if (!response.ok) throw new Error('History export failed.');
    return await response.blob();
  },
  billing: async () => {
    const overview = await apiRequest('/billing', billingSchema);
    try {
      const drafts = await apiRequest('/bill-rate-imports', z.object({ extractions: z.array(rateDraftSchema) }));
      return { ...overview, drafts: drafts.extractions };
    } catch (error) {
      if (error instanceof ApiError && error.status === 403) return { ...overview, drafts: [] };
      throw error;
    }
  },
  alerts: () => apiRequest('/alerts', alertsSchema),
  devices: () => apiRequest('/devices', devicesSchema),
  users: () => apiRequest('/users', z.object({ users: z.array(userSchema) })),
  roles: () => apiRequest('/roles', z.object({ roles: z.array(roleSchema), available_permissions: z.array(z.string()) })),
  health: () => apiRequest('/system/health', systemHealthSchema),
  backups: () => apiRequest('/backups/status', backupStatusSchema),
  homeUtility: () => apiRequest('/settings/home-utility', homeUtilitySchema),
  updateHomeUtility: (payload: Record<string, unknown>) => apiRequest('/settings/home-utility', homeUtilitySchema, { method: 'PATCH', body: jsonBody(payload) }),
  updateDevice: (id: string, payload: { friendly_name?: string; measurement_scope?: string; measurement_scope_confirmation?: string }) => apiRequest(`/devices/${encodeURIComponent(id)}`, z.object({ id: z.string(), friendly_name: z.string(), measurement_scope: z.string() }), { method: 'PATCH', body: jsonBody(payload) }),
  revokeDevice: (id: string) => apiRequest(`/devices/${encodeURIComponent(id)}/revoke`, z.undefined(), { method: 'POST', body: jsonBody({ confirmation: 'REVOKE SENSOR' }) }),
  rotateDeviceCredential: (id: string) => apiRequest(`/devices/${encodeURIComponent(id)}/credentials/rotate`, z.object({ rotation: deviceRotationSchema }), { method: 'POST', body: jsonBody({ idempotency_key: idempotencyKey(), typed_confirmation: 'ROTATE SENSOR CREDENTIALS' }) }),
  cancelDeviceCredentialRotation: (id: string, rotationId: string) => apiRequest(`/devices/${encodeURIComponent(id)}/credentials/rotations/${encodeURIComponent(rotationId)}/cancel`, z.object({ rotation: deviceRotationSchema }), { method: 'POST', body: jsonBody({ idempotency_key: idempotencyKey() }) }),
  createEnrollmentToken: (payload: { home_id: string; friendly_name: string; ct_rating_a: string; pzem_variant: 'pzem004t-v4-classic-candidate'; expires_minutes: number }) => apiRequest('/enrollment-tokens', z.object({ token: z.string(), expires_at: z.string().datetime({ offset: true }) }), { method: 'POST', body: jsonBody(payload) }),
  circuits: () => apiRequest('/circuits', z.object({ circuits: z.array(circuitSchema) })),
  createVerifiedAggregate: (payload: { home_id: string; name: string; device_ids: string[]; confirmation: 'I VERIFIED THESE NON-OVERLAPPING METERS' }) => apiRequest('/circuits/verified-aggregates', z.object({ id: z.string(), name: z.string(), device_ids: z.array(z.string()) }), { method: 'POST', body: jsonBody(payload) }),
  acknowledgeAlert: (id: string) => apiRequest(`/alerts/${encodeURIComponent(id)}/acknowledge`, z.object({ id: z.string(), state: z.string() }), { method: 'POST', body: '{}' }),
  silenceAlert: (id: string, until: string) => apiRequest(`/alerts/${encodeURIComponent(id)}/silence`, z.object({ id: z.string(), silenced_until: z.string().datetime({ offset: true }) }), { method: 'POST', body: jsonBody({ until }) }),
  command: (deviceId: string, commandType: string, payload: Record<string, unknown> = {}, prepare?: { commandId: string; confirmationToken: string; typedConfirmation: string }) => apiRequest(`/devices/${encodeURIComponent(deviceId)}/commands`, commandSchema, {
    method: 'POST',
    body: jsonBody({ command_type: commandType, idempotency_key: idempotencyKey(), payload, ...(prepare ? { prepare_command_id: prepare.commandId, confirmation_token: prepare.confirmationToken, typed_confirmation: prepare.typedConfirmation } : {}) }),
  }),
  uploadRatePdf: async (file: File) => {
    const body = new FormData(); body.set('document', file, file.name);
    const response = await apiRequest('/bill-rate-imports', z.object({ extraction: rateDraftSchema, usage_source_notice: z.string(), ignored_prohibited_categories: z.array(z.string()) }), { method: 'POST', body });
    return response.extraction;
  },
  correctRateDraft: async (id: string, field: string, correctedValue: string) => {
    const response = await apiRequest(`/bill-rate-imports/${encodeURIComponent(id)}`, z.object({ extraction: rateDraftSchema }), { method: 'PATCH', body: jsonBody({ field, corrected_value: correctedValue }) });
    return response.extraction;
  },
  publishRateDraft: async (id: string, effectiveAt: string, utilityAccountId?: string) => apiRequest(`/bill-rate-imports/${encodeURIComponent(id)}/publish`, z.object({ rate_plan_version: z.object({ id: z.string() }).passthrough() }), {
    method: 'POST', body: jsonBody({ effective_start: effectiveAt, effective_end: null, administrator_confirmed_effective_date: true, assign_to_utility_account_id: utilityAccountId ?? null }),
  }),
  checkRates: () => apiRequest('/rate-sources/check-now', z.object({ run_id: z.string(), state: z.string() }).passthrough(), { method: 'POST', body: '{}' }),
  updateUserRoles: (id: string, roles: string[]) => apiRequest(`/users/${encodeURIComponent(id)}`, z.object({ id: z.string(), enabled: z.boolean(), display_name: z.string() }).passthrough(), { method: 'PATCH', body: jsonBody({ role_names: roles }) }),
  updateUser: (id: string, payload: { role_names: string[]; enabled: boolean }) => apiRequest(`/users/${encodeURIComponent(id)}`, z.object({ id: z.string(), enabled: z.boolean(), display_name: z.string() }).passthrough(), { method: 'PATCH', body: jsonBody(payload) }),
  createUser: (payload: { email: string; display_name: string; password: string; role_names: string[] }) => apiRequest('/users', z.object({ id: z.string(), email: z.string(), display_name: z.string() }), { method: 'POST', body: jsonBody(payload) }),
  firmwareReleases: () => apiRequest('/firmware/releases', z.object({ releases: z.array(firmwareReleaseSchema) })),
  uploadFirmware: async (file: File, fields: { semantic_version: string; build_number: number; board_profile: string; minimum_boot_version: number; minimum_config_version: number; expected_sha256: string; release_notes: string }) => {
    const body = new FormData();
    body.set('image', file, file.name);
    body.set('semantic_version', fields.semantic_version);
    body.set('build_number', String(fields.build_number));
    body.set('board_profile', fields.board_profile);
    body.set('minimum_boot_version', String(fields.minimum_boot_version));
    body.set('minimum_config_version', String(fields.minimum_config_version));
    body.set('expected_sha256', fields.expected_sha256);
    body.set('release_notes', fields.release_notes);
    return apiRequest('/firmware/releases', z.object({ release: firmwareReleaseSchema, manifest_signature: z.string(), physical_certification: z.string() }), { method: 'POST', body });
  },
  deployFirmware: (releaseId: string, deviceIds: string[], rollout: 'immediate' | 'staged') => apiRequest(`/firmware/releases/${encodeURIComponent(releaseId)}/deploy`, z.object({ deployments: z.array(z.object({ id: z.string(), device_id: z.string(), state: z.string() })) }), { method: 'POST', body: jsonBody({ device_ids: deviceIds, rollout }) }),
  exportDiagnostics: async () => {
    const response = await fetch('/api/v1/diagnostics/bundle', { credentials: 'same-origin', headers: { Accept: 'application/zip' } });
    if (!response.ok) throw new Error('Unable to download the redacted diagnostics bundle.');
    return await response.blob();
  },
};
