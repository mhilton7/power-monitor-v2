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
  firmwareReleaseListSchema,
  firmwareReleaseSchema,
  firmwareDeploymentBatchSchema,
  firmwareLifecycleSettingsSchema,
  historySchema,
  homeScopesSchema,
  homeUtilitySchema,
  homeSchema,
  manualRateCandidateResponseSchema,
  rateActivationResponseSchema,
  rateCandidatesSchema,
  rateCheckResultSchema,
  rateDraftSchema,
  ratePublishResponseSchema,
  rateSourceRunsSchema,
  rateSourceStatusSchema,
  sceRateCatalogSchema,
  rateWorkflowResponseSchema,
  userPreferencesSchema,
  roleSchema,
  sessionSchema,
  systemHealthSchema,
  telemetrySettingsSchema,
  userSchema,
} from './schemas';

export interface ManualRatePeriodInput {
  season: 'summer' | 'winter' | 'all';
  day_type: 'weekday' | 'weekend' | 'holiday' | 'all';
  period_name: string;
  start_minute: number;
  end_minute: number;
  price_per_kwh: string;
}

export interface ManualRateCandidateInput {
  source_title: string;
  tariff_identifier: string;
  source_url?: string;
  administrator_attests_official_source: true;
  rate_plan_name: string;
  rate_class: string;
  effective_start: string;
  effective_end?: string;
  daily_fixed_charge: string;
  monthly_fixed_charge: string;
  baseline_credit_per_kwh: string;
  periods: ManualRatePeriodInput[];
}

export interface RateCandidateReviewInput {
  selected_plan_name: string;
  effective_start: string;
  effective_end?: string;
  administrator_confirmed_effective_date: true;
  administrator_confirmed_provenance: true;
}

function homePath(path: string, homeId: string): string {
  return `${path}?${new URLSearchParams({ home_id: homeId }).toString()}`;
}

function idempotencyKey(): string {
  return crypto.randomUUID();
}

function exactHome<T extends { home_id: string }>(homeId: string, response: T): T {
  if (response.home_id !== homeId) throw new Error('The server returned rate data for a different home.');
  return response;
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
  homeScopes: () => apiRequest('/home-scopes', homeScopesSchema),
  home: (homeId: string) => apiRequest(homePath('/home', homeId), homeSchema),
  history: (query: URLSearchParams) => apiRequest(`/history?${query.toString()}`, historySchema),
  exportHistory: async (query: URLSearchParams) => {
    const exportQuery = new URLSearchParams();
    const from = query.get('from'); const to = query.get('to'); const homeId = query.get('home_id');
    if (from) exportQuery.set('from', from); if (to) exportQuery.set('to', to);
    if (homeId) exportQuery.set('home_id', homeId);
    const response = await fetch(`/api/v1/history/export.csv?${exportQuery.toString()}`, { credentials: 'same-origin', headers: { Accept: 'text/csv' } });
    if (!response.ok) throw new Error('History export failed.');
    return await response.blob();
  },
  billing: async (homeId: string) => {
    const overview = await apiRequest(homePath('/billing', homeId), billingSchema);
    try {
      const drafts = await apiRequest(homePath('/bill-rate-imports', homeId), z.object({ extractions: z.array(rateDraftSchema) }));
      return { ...overview, drafts: drafts.extractions.map((draft) => exactHome(homeId, draft)) };
    } catch (error) {
      if (error instanceof ApiError && error.status === 403) return { ...overview, drafts: [] };
      throw error;
    }
  },
  alerts: () => apiRequest('/alerts', alertsSchema),
  devices: (homeId: string) => apiRequest(homePath('/devices', homeId), devicesSchema),
  users: () => apiRequest('/users', z.object({ users: z.array(userSchema) })),
  roles: () => apiRequest('/roles', z.object({ roles: z.array(roleSchema), available_permissions: z.array(z.string()) })),
  health: () => apiRequest('/system/health', systemHealthSchema),
  backups: () => apiRequest('/backups/status', backupStatusSchema),
  homeUtility: (homeId: string) => apiRequest(homePath('/settings/home-utility', homeId), homeUtilitySchema),
  updateHomeUtility: (homeId: string, payload: Record<string, unknown>) => apiRequest(homePath('/settings/home-utility', homeId), homeUtilitySchema, { method: 'PATCH', body: jsonBody(payload) }),
  telemetrySettings: (homeId: string) => apiRequest(homePath('/settings/telemetry', homeId), telemetrySettingsSchema),
  updateTelemetrySettings: (homeId: string, payload: { telemetry_interval_seconds: 2 | 5 | 10 | 15 | 30 | 60; history_interval_seconds: 15 | 30 | 60 | 300 | 900; retention_days: 30 | 90 | 180 | 365 | null; retention_confirmation?: 'DELETE EXPIRED SAVED HISTORY' }) => apiRequest(homePath('/settings/telemetry', homeId), telemetrySettingsSchema, { method: 'PATCH', body: jsonBody(payload) }),
  updateDevice: (id: string, payload: { friendly_name?: string; location?: string | null; notes?: string | null; display_order?: number; include_in_aggregate?: boolean; show_on_dashboard?: boolean; monitoring_enabled?: boolean; measurement_scope?: string; measurement_scope_confirmation?: string }) => apiRequest(`/devices/${encodeURIComponent(id)}`, z.object({ id: z.string(), friendly_name: z.string(), measurement_scope: z.string() }).passthrough(), { method: 'PATCH', body: jsonBody(payload) }),
  revokeDevice: (id: string) => apiRequest(`/devices/${encodeURIComponent(id)}/revoke`, z.undefined(), { method: 'POST', body: jsonBody({ confirmation: 'REVOKE SENSOR' }) }),
  rotateDeviceCredential: (id: string) => apiRequest(`/devices/${encodeURIComponent(id)}/credentials/rotate`, z.object({ rotation: deviceRotationSchema }), { method: 'POST', body: jsonBody({ idempotency_key: idempotencyKey(), typed_confirmation: 'ROTATE SENSOR CREDENTIALS' }) }),
  cancelDeviceCredentialRotation: (id: string, rotationId: string) => apiRequest(`/devices/${encodeURIComponent(id)}/credentials/rotations/${encodeURIComponent(rotationId)}/cancel`, z.object({ rotation: deviceRotationSchema }), { method: 'POST', body: jsonBody({ idempotency_key: idempotencyKey() }) }),
  createEnrollmentToken: (payload: { home_id: string; friendly_name: string; ct_rating_a: string; pzem_variant: 'pzem004t-v4-classic-candidate'; expires_minutes: number }) => apiRequest('/enrollment-tokens', z.object({ token: z.string(), expires_at: z.string().datetime({ offset: true }) }), { method: 'POST', body: jsonBody(payload) }),
  circuits: (homeId: string) => apiRequest(homePath('/circuits', homeId), z.object({ circuits: z.array(circuitSchema) })),
  createVerifiedAggregate: (payload: { home_id: string; name: string; device_ids: string[]; confirmation: 'I VERIFIED THESE NON-OVERLAPPING METERS' }) => apiRequest('/circuits/verified-aggregates', z.object({ id: z.string(), name: z.string(), device_ids: z.array(z.string()) }), { method: 'POST', body: jsonBody(payload) }),
  createCircuit: (payload: { home_id: string; name: string; description?: string | null; purpose: 'electrical_section' | 'whole_home_total'; is_home_total: boolean; is_billing_source?: boolean; device_ids: string[]; confirmation: 'I VERIFIED THESE NON-OVERLAPPING METERS' }) => apiRequest('/circuits', circuitSchema, { method: 'POST', body: jsonBody(payload) }),
  updateCircuit: (id: string, payload: { name?: string; description?: string | null; purpose?: 'electrical_section' | 'whole_home_total'; is_home_total?: boolean; is_billing_source?: boolean; device_ids?: string[]; confirmation?: 'I VERIFIED THESE NON-OVERLAPPING METERS' }) => apiRequest(`/circuits/${encodeURIComponent(id)}`, circuitSchema, { method: 'PATCH', body: jsonBody(payload) }),
  deleteCircuit: (id: string) => apiRequest(`/circuits/${encodeURIComponent(id)}`, z.undefined(), { method: 'DELETE' }),
  acknowledgeAlert: (id: string) => apiRequest(`/alerts/${encodeURIComponent(id)}/acknowledge`, z.object({ id: z.string(), state: z.string() }), { method: 'POST', body: '{}' }),
  silenceAlert: (id: string, until: string) => apiRequest(`/alerts/${encodeURIComponent(id)}/silence`, z.object({ id: z.string(), silenced_until: z.string().datetime({ offset: true }) }), { method: 'POST', body: jsonBody({ until }) }),
  command: (deviceId: string, commandType: string, payload: Record<string, unknown> = {}, prepare?: { commandId: string; confirmationToken: string; typedConfirmation: string }) => apiRequest(`/devices/${encodeURIComponent(deviceId)}/commands`, commandSchema, {
    method: 'POST',
    body: jsonBody({ command_type: commandType, idempotency_key: idempotencyKey(), payload, ...(prepare ? { prepare_command_id: prepare.commandId, confirmation_token: prepare.confirmationToken, typed_confirmation: prepare.typedConfirmation } : {}) }),
  }),
  uploadRatePdf: async (file: File, homeId: string) => {
    const body = new FormData(); body.set('document', file, file.name); body.set('home_id', homeId);
    const response = await apiRequest('/bill-rate-imports', z.object({ extraction: rateDraftSchema, usage_source_notice: z.string(), ignored_prohibited_categories: z.array(z.string()) }), { method: 'POST', body });
    return exactHome(homeId, response.extraction);
  },
  correctRateDraft: async (id: string, field: string, correctedValue: string) => {
    const response = await apiRequest(`/bill-rate-imports/${encodeURIComponent(id)}`, z.object({ extraction: rateDraftSchema }), { method: 'PATCH', body: jsonBody({ field, corrected_value: correctedValue }) });
    return response.extraction;
  },
  deleteRateDraft: (id: string) => apiRequest(`/bill-rate-imports/${encodeURIComponent(id)}`, z.undefined(), { method: 'DELETE' }),
  publishRateDraft: async (id: string, effectiveAt: string, effectiveEnd: string | null, utilityAccountId?: string) => apiRequest(`/bill-rate-imports/${encodeURIComponent(id)}/publish`, z.object({ rate_plan_version: z.object({ id: z.string() }).passthrough() }), {
    method: 'POST', body: jsonBody({ effective_start: effectiveAt, effective_end: effectiveEnd, administrator_confirmed_effective_date: true, assign_to_utility_account_id: utilityAccountId ?? null }),
  }),
  rateSourceStatus: async (homeId: string) => exactHome(homeId, await apiRequest(homePath('/rate-sources/status', homeId), rateSourceStatusSchema)),
  rateSourceCandidates: async (homeId: string) => exactHome(homeId, await apiRequest(homePath('/rate-sources/candidates', homeId), rateCandidatesSchema)),
  rateSourceRuns: async (homeId: string) => exactHome(homeId, await apiRequest(homePath('/rate-sources/runs', homeId), rateSourceRunsSchema)),
  sceRateCatalog: async (homeId: string) => exactHome(homeId, await apiRequest(homePath('/rate-sources/catalog', homeId), sceRateCatalogSchema)),
  createManualRateCandidate: async (homeId: string, payload: ManualRateCandidateInput) => exactHome(homeId, await apiRequest(homePath('/rate-sources/manual-candidates', homeId), manualRateCandidateResponseSchema, { method: 'POST', body: jsonBody(payload) })),
  reviewRateCandidate: async (homeId: string, candidateId: string, payload: RateCandidateReviewInput) => exactHome(homeId, await apiRequest(homePath(`/rate-sources/candidates/${encodeURIComponent(candidateId)}/review`, homeId), rateWorkflowResponseSchema, { method: 'POST', body: jsonBody(payload) })),
  rejectRateCandidate: async (homeId: string, candidateId: string) => exactHome(homeId, await apiRequest(homePath(`/rate-sources/candidates/${encodeURIComponent(candidateId)}/reject`, homeId), rateWorkflowResponseSchema, { method: 'POST' })),
  deleteRateCandidate: (homeId: string, candidateId: string) => apiRequest(homePath(`/rate-sources/candidates/${encodeURIComponent(candidateId)}`, homeId), z.undefined(), { method: 'DELETE' }),
  publishRateCandidate: async (homeId: string, candidateId: string) => exactHome(homeId, await apiRequest(homePath(`/rate-sources/candidates/${encodeURIComponent(candidateId)}/publish`, homeId), ratePublishResponseSchema, { method: 'POST' })),
  activateRateCandidate: async (homeId: string, candidateId: string, utilityAccountId: string) => exactHome(homeId, await apiRequest(homePath(`/rate-sources/candidates/${encodeURIComponent(candidateId)}/activate`, homeId), rateActivationResponseSchema, { method: 'POST', body: jsonBody({ utility_account_id: utilityAccountId }) })),
  checkRates: (homeId: string) => apiRequest(homePath('/rate-sources/check-now', homeId), rateCheckResultSchema, { method: 'POST', body: '{}' }),
  updateUserRoles: (id: string, roles: string[]) => apiRequest(`/users/${encodeURIComponent(id)}`, z.object({ id: z.string(), enabled: z.boolean(), display_name: z.string() }).passthrough(), { method: 'PATCH', body: jsonBody({ role_names: roles }) }),
  updateUser: (id: string, payload: { email?: string; display_name?: string; role_names?: string[]; enabled?: boolean }) => apiRequest(`/users/${encodeURIComponent(id)}`, z.object({ id: z.string(), email: z.string(), enabled: z.boolean(), display_name: z.string() }).passthrough(), { method: 'PATCH', body: jsonBody(payload) }),
  createUser: (payload: { email: string; display_name: string; password: string; role_names: string[] }) => apiRequest('/users', z.object({ id: z.string(), email: z.string(), display_name: z.string() }), { method: 'POST', body: jsonBody(payload) }),
  resetUserPassword: (id: string, newPassword: string) => apiRequest(`/users/${encodeURIComponent(id)}/reset-password`, z.undefined(), { method: 'POST', body: jsonBody({ new_password: newPassword }) }),
  deleteUser: (id: string) => apiRequest(`/users/${encodeURIComponent(id)}`, z.undefined(), { method: 'DELETE' }),
  restoreUser: (id: string) => apiRequest(`/users/${encodeURIComponent(id)}/restore`, z.object({ id: z.string(), enabled: z.boolean() }), { method: 'POST' }),
  profile: () => apiRequest('/auth/profile', userSchema.extend({ preferences: z.record(z.string(), z.unknown()) })),
  updateProfile: (payload: { display_name?: string; email?: string; current_password?: string }) => apiRequest('/auth/profile', z.object({ id: z.string(), email: z.string(), display_name: z.string(), session_revoked: z.boolean() }), { method: 'PATCH', body: jsonBody(payload) }),
  changePassword: (currentPassword: string, newPassword: string) => apiRequest('/auth/change-password', z.undefined(), { method: 'POST', body: jsonBody({ current_password: currentPassword, new_password: newPassword }) }),
  preferences: async () => (await apiRequest('/auth/preferences', z.object({ preferences: userPreferencesSchema }))).preferences,
  updatePreferences: async (payload: z.infer<typeof userPreferencesSchema>) => (await apiRequest('/auth/preferences', z.object({ preferences: userPreferencesSchema }), { method: 'PUT', body: jsonBody(payload) })).preferences,
  firmwareReleases: (options?: { showArchived?: boolean; showDeleted?: boolean; showDeploymentHistory?: boolean }) => {
    const query = new URLSearchParams();
    if (options?.showArchived) query.set('show_archived', 'true');
    if (options?.showDeleted) query.set('show_deleted', 'true');
    if (options?.showDeploymentHistory) query.set('show_deployment_history', 'true');
    const suffix = query.toString();
    return apiRequest(`/firmware/releases${suffix ? `?${suffix}` : ''}`, firmwareReleaseListSchema);
  },
  archiveFirmwareRelease: (releaseId: string) => apiRequest(`/firmware/releases/${encodeURIComponent(releaseId)}/archive`, firmwareReleaseSchema, { method: 'POST', body: jsonBody({ confirmation: 'ARCHIVE FIRMWARE RECORD' }) }),
  restoreFirmwareRelease: (releaseId: string) => apiRequest(`/firmware/releases/${encodeURIComponent(releaseId)}/restore`, firmwareReleaseSchema, { method: 'POST', body: jsonBody({ confirmation: 'RESTORE FIRMWARE RECORD' }) }),
  makeFirmwareReleaseCurrent: (release: { release_id: string; semantic_version: string; sha256: string }) => apiRequest(`/firmware/releases/${encodeURIComponent(release.release_id)}/make-current`, firmwareReleaseSchema, { method: 'POST', body: jsonBody({ confirmation: 'MAKE CURRENT FIRMWARE', semantic_version: release.semantic_version, sha256: release.sha256 }) }),
  updateFirmwareRollbackProtection: (releaseId: string, rollbackPinned: boolean) => apiRequest(`/firmware/releases/${encodeURIComponent(releaseId)}/rollback-pin`, firmwareReleaseSchema, { method: 'PATCH', body: jsonBody({ confirmation: 'UPDATE ROLLBACK PROTECTION', rollback_pinned: rollbackPinned }) }),
  deleteFirmwareReleasePermanently: (release: { release_id: string; semantic_version: string; build_number: number; sha256: string }) => apiRequest(`/firmware/releases/${encodeURIComponent(release.release_id)}/delete-permanently`, z.undefined(), { method: 'POST', body: jsonBody({ confirmation: 'DELETE RELEASE PERMANENTLY', semantic_version: release.semantic_version, build_number: String(release.build_number), sha256: release.sha256 }) }),
  firmwareDeploymentBatches: (options?: { showArchived?: boolean; showDeleted?: boolean }) => {
    const query = new URLSearchParams();
    if (options?.showArchived) query.set('show_archived', 'true');
    if (options?.showDeleted) query.set('show_deleted', 'true');
    const suffix = query.toString();
    return apiRequest(`/firmware/deployment-batches${suffix ? `?${suffix}` : ''}`, z.object({ deployment_batches: z.array(firmwareDeploymentBatchSchema) }));
  },
  archiveFirmwareDeployment: (batchId: string) => apiRequest(`/firmware/deployment-batches/${encodeURIComponent(batchId)}/archive`, firmwareDeploymentBatchSchema, { method: 'POST', body: jsonBody({ confirmation: 'ARCHIVE DEPLOYMENT RECORD' }) }),
  restoreFirmwareDeployment: (batchId: string) => apiRequest(`/firmware/deployment-batches/${encodeURIComponent(batchId)}/restore`, firmwareDeploymentBatchSchema, { method: 'POST', body: jsonBody({ confirmation: 'RESTORE DEPLOYMENT RECORD' }) }),
  deleteFirmwareDeploymentPermanently: (batchId: string) => apiRequest(`/firmware/deployment-batches/${encodeURIComponent(batchId)}/delete-permanently`, z.undefined(), { method: 'POST', body: jsonBody({ confirmation: 'DELETE DEPLOYMENT RECORD', deployment_batch_id: batchId }) }),
  firmwareLifecycleSettings: () => apiRequest('/firmware/lifecycle-settings', firmwareLifecycleSettingsSchema),
  updateFirmwareLifecycleSettings: (deploymentRetentionDays: 90 | 180 | 365 | null) => apiRequest('/firmware/lifecycle-settings', firmwareLifecycleSettingsSchema, { method: 'PATCH', body: jsonBody({ deployment_retention_days: deploymentRetentionDays, ...(deploymentRetentionDays === null ? {} : { confirmation: 'DELETE EXPIRED DEPLOYMENT HISTORY' }) }) }),
  uploadFirmware: async (file: File, fields: { semantic_version: string; build_number: number; board_profile: string; minimum_boot_version: number; minimum_config_version: number; expected_sha256: string; firmware_build_id?: string; release_notes: string }) => {
    const body = new FormData();
    body.set('image', file, file.name);
    body.set('semantic_version', fields.semantic_version);
    body.set('build_number', String(fields.build_number));
    body.set('board_profile', fields.board_profile);
    body.set('minimum_boot_version', String(fields.minimum_boot_version));
    body.set('minimum_config_version', String(fields.minimum_config_version));
    body.set('expected_sha256', fields.expected_sha256);
    if (fields.firmware_build_id) body.set('firmware_build_id', fields.firmware_build_id);
    body.set('release_notes', fields.release_notes);
    return apiRequest('/firmware/releases', z.object({ release: firmwareReleaseSchema, manifest_signature: z.string(), physical_certification: z.string() }), { method: 'POST', body });
  },
  deployFirmware: (releaseId: string, deviceIds: string[], rollout: 'immediate' | 'staged') => apiRequest(`/firmware/releases/${encodeURIComponent(releaseId)}/deploy`, z.object({ batch_id: z.string(), batch_state: z.string(), deployments: z.array(z.object({ id: z.string(), device_id: z.string(), state: z.string() })) }), { method: 'POST', body: jsonBody({ device_ids: deviceIds, rollout }) }),
  retryFirmwareBatch: (batchId: string, deviceIds: string[]) => apiRequest(`/firmware/deployment-batches/${encodeURIComponent(batchId)}/retry`, z.object({ batch_id: z.string(), batch_state: z.string(), deployments: z.array(z.object({ id: z.string(), device_id: z.string(), state: z.string() })) }), { method: 'POST', body: jsonBody({ device_ids: deviceIds }) }),
  cancelFirmwareBatch: (batchId: string) => apiRequest(`/firmware/deployment-batches/${encodeURIComponent(batchId)}/cancel`, z.object({ batch_id: z.string(), state: z.string() }), { method: 'POST' }),
  deleteFirmwareArtifact: (releaseId: string) => apiRequest(`/firmware/releases/${encodeURIComponent(releaseId)}`, z.undefined(), { method: 'DELETE' }),
  exportDiagnostics: async () => {
    const response = await fetch('/api/v1/diagnostics/bundle', { credentials: 'same-origin', headers: { Accept: 'application/zip' } });
    if (!response.ok) throw new Error('Unable to download the redacted diagnostics bundle.');
    return await response.blob();
  },
};
