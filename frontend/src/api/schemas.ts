import { z } from 'zod';

const isoDate = z.string().datetime({ offset: true });
const decimal = z.union([z.string(), z.number().finite()]);
const nullableDecimal = decimal.nullable();
const nullableNumber = z.number().finite().nullable();

export const userSchema = z.object({
  id: z.string(),
  display_name: z.string(),
  email: z.string(),
  permissions: z.array(z.string()).default([]),
  roles: z.array(z.string()).default([]),
  mfa_enabled: z.boolean().default(false),
  enabled: z.boolean().default(true),
  deleted_at: isoDate.nullable().optional(),
}).passthrough();

export const sessionSchema = z.object({
  authenticated: z.boolean(),
  bootstrap_required: z.boolean().default(false),
  user: userSchema.nullable().default(null),
});

export const measurementSchema = z.object({
  voltage_v: nullableNumber,
  current_a: nullableNumber,
  active_power_w: nullableNumber,
  frequency_hz: nullableNumber,
  power_factor: nullableNumber,
  measured_at: isoDate.nullable(),
  pzem_status: z.string().optional(),
}).passthrough();

export const deviceSummarySchema = z.object({
  id: z.string(),
  friendly_name: z.string(),
  state: z.enum(['live', 'waiting', 'stale', 'offline', 'unavailable', 'invalid', 'needs_attention']),
  measurement: measurementSchema.nullable(),
  heartbeat_at: isoDate.nullable(),
  last_committed_at: isoDate.nullable(),
  backlog: z.number().int().nonnegative().nullable(),
  storage_status: z.string().optional(),
  firmware_version: z.string().nullable().optional(),
  measurement_scope: z.string().optional(),
  estimated_cost_per_hour: nullableDecimal.optional(),
}).passthrough();

export const energyCostSummarySchema = z.object({
  energy_kwh: nullableDecimal,
  cost: nullableDecimal,
  completeness: nullableDecimal,
  missing_intervals: z.number().int().nonnegative().default(0),
}).passthrough();

export const homeSchema = z.object({
  generated_at: isoDate.optional(),
  devices: z.array(deviceSummarySchema),
  summaries: z.object({
    today: energyCostSummarySchema,
    week: energyCostSummarySchema,
    billing_cycle: energyCostSummarySchema,
  }).passthrough(),
  current_rate: z.object({
    plan_name: z.string(),
    version_id: z.string(),
    effective_start: isoDate,
    period: z.string().nullable(),
    price_per_kwh: nullableDecimal,
    period_start_minute: z.number().int().nullable(),
    period_end_minute: z.number().int().nullable(),
    next_change_at: isoDate.nullable().optional(),
    scope: z.string(),
    fixed_charges_included: z.boolean(),
    baseline_credit_included: z.boolean(),
    cca_or_direct_access: z.string().nullable(),
  }).passthrough().nullable(),
  summary_scope: z.object({ kind: z.string(), device_id: z.string().nullable(), device_ids: z.array(z.string()).optional(), aggregate: z.boolean(), circuit_id: z.string().nullable().optional() }).optional(),
  disclosure: z.object({ usage_source: z.string(), estimated_not_utility_bill: z.boolean() }).optional(),
}).passthrough();

export const historyPointSchema = z.object({
  timestamp: isoDate,
  value: nullableDecimal,
  cost: nullableDecimal,
  quality: nullableDecimal,
}).passthrough();

export const historySchema = z.object({
  points: z.array(historyPointSchema),
  energy_kwh: nullableDecimal,
  cost: nullableDecimal,
  completeness: nullableDecimal,
  missing_ranges: z.array(z.object({ start: isoDate, end: isoDate }).passthrough()).default([]),
  resolution_seconds: z.number().int().positive(),
  timezone: z.string(),
  usage_source: z.string(),
  scope: z.object({ device_ids: z.array(z.string()), aggregate: z.boolean() }).optional(),
}).passthrough();

export const rateEvidenceSchema = z.object({
  field: z.string().optional(),
  key: z.string().optional(),
  label: z.string().optional(),
  value: z.unknown().optional(),
  source: z.object({ page: z.number().int().positive(), region: z.unknown().optional() }).optional(),
  confidence: nullableDecimal.optional(),
}).passthrough();

export const rateDraftSchema = z.object({
  id: z.string(),
  artifact_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  utility_name: z.string().nullable(),
  rate_plan_name: z.string().nullable(),
  rate_class: z.string().nullable(),
  cca_or_direct_access_indicator: z.string().nullable(),
  season_definitions: z.array(z.unknown()),
  day_type_definitions: z.array(z.unknown()),
  tou_period_definitions: z.array(z.unknown()),
  tier_threshold_definitions: z.array(z.unknown()),
  reusable_price_components: z.array(z.unknown()),
  baseline_allocation_rule: z.string().nullable(),
  baseline_credit_rate: nullableDecimal,
  effective_start_candidate: isoDate.nullable(),
  effective_end_candidate: isoDate.nullable(),
  source_evidence: z.array(rateEvidenceSchema),
  parser_version: z.string(),
  state: z.enum(['review_required', 'approved', 'rejected', 'published']),
  resulting_rate_version_id: z.string().nullable(),
  review_required: z.boolean(),
}).strict();

export const billingSchema = z.object({
  accounts: z.array(z.object({
    utility_account_id: z.string(),
    plan_name: z.string().nullable(),
    rate_version_id: z.string().nullable(),
    effective_start: isoDate.nullable(),
    cost_scope: z.enum(['energy_only', 'allocated_account', 'full_account']),
    baseline_credit_included: z.boolean(),
    fixed_charges_included: z.boolean(),
    cca_or_direct_access: z.string().nullable(),
    estimates: z.array(z.object({
      id: z.string(),
      kind: z.string(),
      scope_kind: z.enum(['energy_only', 'allocated_account', 'full_account']),
      scope_id: z.string(),
      member_device_ids: z.array(z.string()),
      rate_plan_version_id: z.string(),
      scope_start_utc: isoDate,
      scope_end_utc: isoDate,
      sensor_energy_kwh: decimal,
      energy_cost: decimal,
      fixed_charge: decimal,
      credits: decimal,
      total: decimal,
      completeness: decimal,
      missing_intervals: z.number().int().nonnegative(),
      calculated_at: isoDate,
    })).default([]),
  }).passthrough()),
  usage_source: z.string(),
  rate_import_notice: z.string(),
  drafts: z.array(rateDraftSchema).default([]),
}).passthrough();

export const alertSchema = z.object({
  id: z.string(),
  type: z.string(),
  severity: z.enum(['info', 'warning', 'critical']),
  state: z.enum(['open', 'acknowledged', 'silenced', 'resolved']),
  opened_at: isoDate,
  evidence: z.record(z.string(), z.unknown()),
}).passthrough();

export const alertsSchema = z.object({ alerts: z.array(alertSchema) }).transform((value) => ({
  ...value,
  active_count: value.alerts.filter((alert) => alert.state === 'open').length,
}));

export const deviceRotationSchema = z.object({
  rotation_id: z.string(),
  credential_fingerprint: z.string().length(64),
  state: z.enum(['pending', 'prepared', 'active', 'revoked']),
  overlap_expires_at: isoDate,
  prepare_command_id: z.string().nullable(),
  commit_command_id: z.string().nullable(),
  cancel_command_id: z.string().nullable(),
});

export const deviceDetailSchema = z.object({
  id: z.string(),
  home_id: z.string(),
  circuit_id: z.string().nullable(),
  friendly_name: z.string(),
  device_fingerprint: z.string(),
  credential_fingerprint: z.string().length(64).nullable(),
  credential_key_version: z.number().int().positive().nullable(),
  credential_rotation: deviceRotationSchema.nullable(),
  firmware_version: z.string().nullable(),
  protocol: z.string(),
  pzem_variant: z.string(),
  ct_rating_a: nullableDecimal,
  measurement_scope: z.enum(['energy_only', 'allocated_account', 'full_account']),
  heartbeat_at: isoDate.nullable(),
  wifi_rssi: nullableNumber,
  ip_address: z.string().nullable(),
  pzem_status: z.string(),
  storage_status: z.string(),
  oldest_sequence: z.number().int().nonnegative().nullable(),
  newest_sequence: z.number().int().nonnegative().nullable(),
  acknowledgement: z.number().int().nonnegative(),
  backlog: z.number().int().nonnegative().nullable(),
  free_internal_heap: z.number().int().nonnegative().nullable(),
  largest_internal_block: z.number().int().nonnegative().nullable(),
  last_reboot_reason: z.string().nullable(),
  last_command: z.object({ id: z.string(), type: z.string(), state: z.string(), progress_percent: z.number().min(0).max(100), expires_at: z.string().datetime({ offset: true }).optional(), result_code: z.string().nullable().optional(), result_evidence: z.record(z.string(), z.union([z.string(), z.number(), z.boolean(), z.null()])).optional() }).nullable(),
}).passthrough();
export const devicesSchema = z.object({
  home_scopes: z.array(z.object({ id: z.string().length(36), name: z.string().min(1) })).default([]),
  devices: z.array(deviceDetailSchema),
});

export const commandSchema = z.object({
  command: z.object({ id: z.string(), type: z.string(), state: z.string(), expires_at: isoDate.optional() }).passthrough(),
  confirmation_token: z.string().nullable().optional(),
});

export const roleSchema = z.object({ id: z.string(), name: z.string(), built_in: z.boolean(), permissions: z.array(z.string()), description: z.string().optional() }).passthrough();

export const homeUtilitySchema = z.object({
  home: z.object({ id: z.string(), name: z.string(), timezone: z.string() }),
  utility: z.object({
    id: z.string(),
    utility_name: z.string(),
    timezone: z.string(),
    billing_day: z.number().int().min(1).max(28),
    cost_scope: z.enum(['energy_only', 'allocated_account', 'full_account']),
    baseline_allocation_kwh: nullableDecimal,
    cca_provider: z.string().nullable(),
  }),
  usage_source: z.string(),
}).passthrough();

export const firmwareReleaseSchema = z.object({
  schema: z.literal('pm-ota-manifest/1.0.0'),
  release_id: z.string(),
  semantic_version: z.string(),
  build_number: z.number().int().positive(),
  project_name: z.string(),
  target_chip: z.string(),
  board_profile: z.string(),
  minimum_boot_version: z.number().int().positive(),
  minimum_protocol: z.string(),
  minimum_config_version: z.number().int().positive(),
  image_size: z.number().int().positive(),
  sha256: z.string().regex(/^[a-f0-9]{64}$/),
  candidate: z.boolean(),
  release_notes: z.string().optional(),
  physical_certification: z.string().optional(),
}).passthrough();

export const circuitSchema = z.object({
  id: z.string(),
  home_id: z.string(),
  name: z.string(),
  aggregate_mode: z.string(),
}).passthrough();

export const systemHealthSchema = z.object({
  generated_at: isoDate,
  version: z.string(),
  protocol: z.string(),
  database: z.string(),
  sensors: z.array(z.object({ device_id: z.string(), state: z.string(), heartbeat_age_seconds: nullableNumber, pzem_status: z.string(), storage_status: z.string(), backlog: z.number().nullable() }).passthrough()),
  open_alert_count: z.number().int().nonnegative(),
  last_rate_sync: z.object({ id: z.string(), state: z.string(), event_code: z.string(), completed_at: isoDate.nullable() }).nullable(),
  physical_hardware_certification: z.string(),
}).passthrough();

const evidenceSchema = z.record(z.string(), z.unknown());
export const backupStatusSchema = z.object({
  last_successful_backup: evidenceSchema,
  last_backup_attempt: evidenceSchema,
  last_successful_restore_test: evidenceSchema,
  last_restore_test_attempt: evidenceSchema,
  verification_rule: z.string(),
}).passthrough();

export type Session = z.infer<typeof sessionSchema>;
export type User = z.infer<typeof userSchema>;
export type HomeData = z.infer<typeof homeSchema>;
export type HistoryData = z.infer<typeof historySchema>;
export type BillingData = z.infer<typeof billingSchema>;
export type RateDraft = z.infer<typeof rateDraftSchema>;
export type Alert = z.infer<typeof alertSchema>;
export type DeviceDetail = z.infer<typeof deviceDetailSchema>;
export type Command = z.infer<typeof commandSchema>;
export type SystemHealth = z.infer<typeof systemHealthSchema>;
export type BackupStatus = z.infer<typeof backupStatusSchema>;
export type HomeUtility = z.infer<typeof homeUtilitySchema>;
export type FirmwareRelease = z.infer<typeof firmwareReleaseSchema>;
export type Circuit = z.infer<typeof circuitSchema>;
