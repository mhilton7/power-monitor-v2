import { z } from 'zod';

const isoDate = z.string().datetime({ offset: true });
const localDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/);
const rateEffectiveDate = z.union([isoDate, localDate]);
const decimal = z.union([z.string(), z.number().finite()]);
const nullableDecimal = decimal.nullable();
const nullableNumber = z.number().finite().nullable();
const nullableTelemetryNumber = z.union([
  z.number().finite().nonnegative(),
  z.string()
    .regex(/^(?:0|[1-9]\d*)(?:\.\d+)?$/)
    .transform(Number)
    .pipe(z.number().finite().nonnegative()),
]).nullable();

export const userSchema = z.object({
  id: z.string(),
  display_name: z.string(),
  email: z.string(),
  permissions: z.array(z.string()).default([]),
  roles: z.array(z.string()).default([]),
  mfa_enabled: z.boolean().default(false),
  enabled: z.boolean().default(true),
  deleted_at: isoDate.nullable().optional(),
  created_at: isoDate.optional(),
  last_login_at: isoDate.nullable().optional(),
  manageable: z.boolean().default(false),
}).passthrough();

export const userPreferencesSchema = z.object({
  dashboard_range: z.enum(['today', 'week', 'month']),
  history_range: z.enum(['day', 'week', 'month', 'billing_cycle']),
  refresh_seconds: z.union([z.literal(15), z.literal(30), z.literal(60), z.literal(120), z.literal(300)]),
  power_unit: z.enum(['auto', 'W', 'kW']),
  energy_unit: z.enum(['auto', 'Wh', 'kWh']),
  date_format: z.enum(['iso', 'us']),
  time_format: z.enum(['12h', '24h']),
  decimal_precision: z.number().int().min(0).max(4),
  density: z.enum(['comfortable', 'compact']),
  dashboard_cards: z.array(z.enum(['live_power', 'energy', 'cost', 'completeness', 'alerts'])).min(1),
}).strict();

export const sessionSchema = z.object({
  authenticated: z.boolean(),
  bootstrap_required: z.boolean().default(false),
  user: userSchema.nullable().default(null),
});

export const measurementSchema = z.object({
  voltage_v: nullableTelemetryNumber,
  current_a: nullableTelemetryNumber,
  active_power_w: nullableTelemetryNumber,
  frequency_hz: nullableTelemetryNumber,
  power_factor: nullableTelemetryNumber,
  measured_at: isoDate.nullable(),
  pzem_status: z.string().optional(),
}).passthrough();

export const deviceSummarySchema = z.object({
  id: z.string(),
  friendly_name: z.string(),
  location: z.string().nullable().optional(),
  notes: z.string().nullable().optional(),
  display_order: z.number().int().nonnegative().optional(),
  include_in_aggregate: z.boolean().optional(),
  show_on_dashboard: z.boolean().optional(),
  monitoring_enabled: z.boolean().optional(),
  state: z.enum(['live', 'waiting', 'stale', 'offline', 'unavailable', 'invalid', 'needs_attention', 'monitoring_disabled']),
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
  aggregate_measurement: z.object({
    state: z.enum(['live', 'unavailable']),
    active_power_w: nullableTelemetryNumber,
    member_device_ids: z.array(z.string()),
    voltage_v: nullableTelemetryNumber,
    frequency_hz: nullableTelemetryNumber,
    power_factor: nullableTelemetryNumber,
  }).nullable().optional(),
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
  name: z.string().optional(),
  field: z.string().optional(),
  key: z.string().optional(),
  normalized_value: z.string().optional(),
  supporting_label: z.string().nullable().optional(),
  label: z.string().optional(),
  value: z.unknown().optional(),
  source: z.object({ page: z.number().int().positive(), region: z.unknown().optional() }).optional(),
  confidence: nullableDecimal.optional(),
}).passthrough();

export const rateDraftSchema = z.object({
  id: z.string(),
  home_id: z.string().length(36),
  artifact_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  utility_name: z.string().nullable(),
  rate_plan_name: z.string().nullable(),
  rate_class: z.string().nullable(),
  plan_classification: z.enum(['flat', 'tiered', 'seasonal_tiered', 'time_of_use', 'unknown']),
  holiday_treatment: z.enum(['not_applicable', 'no_special_treatment', 'weekend_schedule', 'explicit_schedule', 'unresolved']),
  cca_or_direct_access_indicator: z.string().nullable(),
  season_definitions: z.array(z.unknown()),
  day_type_definitions: z.array(z.unknown()),
  tou_period_definitions: z.array(z.unknown()),
  tier_threshold_definitions: z.array(z.unknown()),
  tier_threshold_rule: z.object({
    rule_type: z.literal('daily_allowance'),
    season: z.enum(['summer', 'winter']),
    kwh_per_day: nullableDecimal,
    source_allowance_kwh: decimal,
    source_billing_days: z.number().int().min(1).max(62).nullable(),
    tier1_boundary_inclusive: z.literal(true),
  }).strict().nullable().default(null),
  reusable_price_components: z.array(z.unknown()),
  billing_period_start: localDate.nullable(),
  billing_period_end: localDate.nullable(),
  billing_period_days: z.number().int().positive().nullable(),
  tier_threshold_basis: z.string().nullable(),
  candidate_complete: z.boolean(),
  publication_scope: z.enum(['complete_schedule', 'bill_period_only', 'review_only']),
  publishable_effective_start: isoDate.nullable(),
  publishable_effective_end: isoDate.nullable(),
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

const rateWorkflowStateSchema = z.enum(['review_required', 'reviewed', 'published', 'activated', 'rejected']);

export const rateCandidateWorkflowSchema = z.object({
  id: z.string().optional(),
  state: rateWorkflowStateSchema,
  selected_plan_name: z.string().nullable().optional(),
  effective_start: isoDate.nullable().optional(),
  effective_end: isoDate.nullable().optional(),
  reviewed_at: isoDate.nullable().optional(),
  published_at: isoDate.nullable().optional(),
  activated_at: isoDate.nullable().optional(),
  rate_plan_version_id: z.string().nullable().optional(),
  utility_account_id: z.string().nullable().optional(),
}).strict();

export const rateCandidatePeriodSchema = z.object({
  season: z.enum(['summer', 'winter', 'all']),
  day_type: z.enum(['weekday', 'weekend', 'holiday', 'weekend_holiday', 'all_days', 'all']),
  name: z.string(),
  start_minute: z.number().int().min(0).max(1439),
  end_minute: z.number().int().min(1).max(1440),
  price_per_kwh: decimal,
  currency: z.literal('USD'),
  unit: z.literal('kWh'),
  tier_min_kwh: nullableDecimal,
  tier_max_kwh: nullableDecimal,
}).strict();

export const normalizedRatePlanSchema = z.object({
  rate_plan_name: z.string(),
  rate_class: z.string(),
  pricing_model: z.enum(['flat', 'tiered', 'seasonal_tiered', 'time_of_use', 'time_of_use_plus_baseline_credit']),
  daily_fixed_charge: decimal,
  monthly_fixed_charge: decimal,
  baseline_credit_per_kwh: decimal,
  rate_components: z.enum(['sce_delivery_and_generation_combined', 'administrator_entered_combined_price']),
  tier_threshold_basis: z.string().optional(),
  periods: z.array(rateCandidatePeriodSchema).min(1),
}).strict();

export const normalizedRateCandidateSchema = z.object({
  schema: z.literal('sce-rate-candidate/1.0.0'),
  utility_name: z.literal('Southern California Edison'),
  timezone: z.literal('America/Los_Angeles'),
  currency: z.literal('USD'),
  plan_classification: z.enum(['flat', 'tiered', 'seasonal_tiered', 'time_of_use']).optional(),
  holiday_treatment: z.enum(['not_applicable', 'no_special_treatment', 'weekend_schedule', 'explicit_schedule', 'unresolved']).optional(),
  season_definitions: z.object({
    summer: z.object({ start_month: z.number().int().min(1).max(12), end_month: z.number().int().min(1).max(12) }).strict(),
    winter: z.object({ start_month: z.number().int().min(1).max(12), end_month: z.number().int().min(1).max(12) }).strict(),
  }).strict(),
  holiday_rule: z.enum(['not_applicable', 'weekend_rates', 'administrator_entered_schedule']),
  effective_start: rateEffectiveDate.nullable(),
  effective_end: rateEffectiveDate.nullable(),
  effective_date_confirmation_required: z.literal(true),
  plans: z.array(normalizedRatePlanSchema).min(1),
}).strict();

const rateCandidateValidationSchema = z.object({
  origin: z.literal('manual_administrator_entry').optional(),
  parser_version: z.string(),
  schema: z.literal('sce-rate-candidate/1.0.0'),
  plan_classification: z.enum(['flat', 'tiered', 'seasonal_tiered', 'time_of_use']).optional(),
  holiday_treatment: z.enum(['not_applicable', 'no_special_treatment', 'weekend_schedule', 'explicit_schedule', 'unresolved']).optional(),
  plan_count: z.number().int().positive().optional(),
  period_count: z.number().int().positive().optional(),
  seasons: z.array(z.enum(['summer', 'winter'])).optional(),
  day_types: z.array(z.enum(['weekday', 'weekend', 'holiday', 'weekend_holiday', 'all_days', 'all'])).optional(),
  coverage: z.enum(['complete', 'semantic_tier_coverage']),
  price_unit: z.literal('USD/kWh'),
  effective_date: z.union([rateEffectiveDate, z.enum(['administrator_confirmation_required', 'administrator_review_required'])]),
  warnings: z.array(z.string()).optional(),
  source_artifact_sha256: z.string().regex(/^[a-f0-9]{64}$/).optional(),
  source_revision_id: z.string().optional(),
  source_title: z.string().optional(),
  tariff_identifier: z.string().optional(),
  source_url: z.string().url().nullable().optional(),
  canonical_input_sha256: z.string().regex(/^[a-f0-9]{64}$/).optional(),
  canonical_input_bytes: z.number().int().positive().optional(),
  provenance_confirmation: z.literal('administrator_attested_official_source').optional(),
}).strict();

const rateCandidateDiffSchema = z.object({
  schema: z.literal('sce-rate-diff/1.0.0').optional(),
  previous_candidate_id: z.string().nullable().optional(),
  before: normalizedRateCandidateSchema.nullable().optional(),
  after: normalizedRateCandidateSchema.optional(),
  changes: z.array(z.object({ path: z.string() })).default([]),
  change_count: z.number().int().nonnegative(),
  truncated: z.boolean().optional(),
}).strict();

export const rateCandidateSchema = z.object({
  id: z.string(),
  state: z.enum(['review_required', 'approved', 'rejected', 'published']),
  created_at: isoDate,
  reviewed_at: isoDate.nullable(),
  source: z.object({
    id: z.string(),
    name: z.string(),
    url: z.string().url().nullable(),
    revision_id: z.string(),
    artifact_sha256: z.string().regex(/^[a-f0-9]{64}$/),
    retrieved_at: isoDate,
    parser_version: z.string(),
  }).strict(),
  normalized_rates: normalizedRateCandidateSchema,
  validation_evidence: rateCandidateValidationSchema,
  diff: rateCandidateDiffSchema,
  manual_approval_required: z.literal(true),
  workflow: rateCandidateWorkflowSchema,
}).strict();

export const rateCandidatesSchema = z.object({
  home_id: z.string(),
  candidates: z.array(rateCandidateSchema),
}).strict();

export const rateRunSummarySchema = z.object({
  id: z.string(),
  source_id: z.string(),
  source_name: z.string().nullable(),
  source_type: z.string().nullable(),
  source_url: z.string().url().nullable(),
  state: z.enum(['running', 'review_required', 'unchanged', 'failed']),
  event_code: z.string(),
  started_at: isoDate,
  completed_at: isoDate.nullable(),
  revision_id: z.string().nullable(),
  error_code: z.string().nullable(),
  initiator: z.enum(['user', 'scheduled_worker']).nullable(),
}).strict();

const activeRateSourceSchema = z.discriminatedUnion('state', [
  z.object({ state: z.literal('not_configured') }).strict(),
  z.object({
    state: z.literal('active'),
    utility_account_id: z.string(),
    assignment_id: z.string(),
    rate_plan_version_id: z.string(),
    plan_name: z.string(),
    effective_start: isoDate,
    effective_end: isoDate.nullable(),
    provenance: z.object({
      source_artifact_sha256: z.string().regex(/^[a-f0-9]{64}$/),
      origin: z.string(),
      source_name: z.string().optional(),
      source_url: z.string().url().nullable().optional(),
      source_revision_id: z.string().optional(),
      candidate_id: z.string().optional(),
      review_id: z.string().optional(),
    }).strict(),
  }).strict(),
]);

const lastKnownGoodRateSourceSchema = z.discriminatedUnion('state', [
  z.object({ state: z.literal('unavailable') }).strict(),
  z.object({
    state: z.literal('available'),
    candidate_id: z.string(),
    source_revision_id: z.string(),
    source_artifact_sha256: z.string().regex(/^[a-f0-9]{64}$/),
    retrieved_at: isoDate,
    source_name: z.string(),
    source_type: z.string(),
    source_url: z.string().url().nullable(),
    active_source_match: z.boolean(),
  }).strict(),
]);

export const rateSourceStatusSchema = z.object({
  home_id: z.string(),
  scheduled: z.discriminatedUnion('state', [
    z.object({ state: z.literal('not_configured') }).strict(),
    z.object({
      state: z.enum(['enabled', 'disabled']),
      source_id: z.string(),
      source_name: z.string(),
      source_url: z.string().url(),
      check_interval_hours: z.number().int().positive(),
      next_check_at: isoDate.nullable(),
    }).strict(),
  ]),
  last_run: rateRunSummarySchema.nullable(),
  last_success: rateRunSummarySchema.nullable(),
  last_failure: rateRunSummarySchema.nullable(),
  active: activeRateSourceSchema,
  last_known_good: lastKnownGoodRateSourceSchema,
}).strict();

export const rateSourceRunSchema = z.object({
  id: z.string(),
  source_id: z.string(),
  source_name: z.string(),
  source_type: z.string(),
  state: z.enum(['running', 'review_required', 'unchanged', 'failed']),
  event_code: z.string(),
  correlation_id: z.string(),
  started_at: isoDate,
  completed_at: isoDate.nullable(),
  requested_url: z.string().nullable(),
  final_url: z.string().nullable(),
  http_status: z.number().int().nullable(),
  response_bytes: z.number().int().nonnegative().nullable(),
  revision_id: z.string().nullable(),
  error_code: z.string().nullable(),
  evidence: z.record(z.string(), z.unknown()),
}).strict();

export const rateSourceRunsSchema = z.object({ home_id: z.string(), runs: z.array(rateSourceRunSchema) }).strict();

export const rateCheckResultSchema = z.object({
  run_id: z.string(),
  state: z.enum(['review_required', 'unchanged', 'failed']),
  event_code: z.string(),
  revision_id: z.string().nullable(),
  candidate_id: z.string().nullable(),
  error_code: z.string().nullable(),
}).strict();

export const rateWorkflowResponseSchema = z.object({
  home_id: z.string(),
  candidate_id: z.string(),
  workflow: rateCandidateWorkflowSchema,
}).strict();

export const ratePublishResponseSchema = rateWorkflowResponseSchema.extend({
  rate_plan_version: z.object({
    id: z.string(),
    plan_id: z.string(),
    plan_name: z.string(),
    version: z.number().int().positive(),
    effective_start: isoDate,
    effective_end: isoDate.nullable(),
    source_artifact_sha256: z.string().regex(/^[a-f0-9]{64}$/),
    state: z.literal('published'),
  }).strict(),
}).strict();

export const rateActivationResponseSchema = rateWorkflowResponseSchema.extend({
  assignment: z.object({
    id: z.string(),
    utility_account_id: z.string(),
    rate_plan_version_id: z.string(),
    effective_start: isoDate,
    effective_end: isoDate.nullable(),
  }).strict(),
}).strict();

export const manualRateCandidateResponseSchema = z.object({
  home_id: z.string(),
  created: z.boolean(),
  candidate_id: z.string(),
  revision_id: z.string(),
  source_id: z.string(),
  run_id: z.string(),
  state: z.literal('review_required'),
  canonical_input_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  network_fetch_performed: z.literal(false),
}).strict();

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
  location: z.string().nullable().default(null),
  notes: z.string().nullable().default(null),
  display_order: z.number().int().nonnegative().default(0),
  include_in_aggregate: z.boolean().default(true),
  show_on_dashboard: z.boolean().default(true),
  monitoring_enabled: z.boolean().default(true),
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
  storage_bytes_total: z.number().int().positive().nullable(),
  storage_bytes_free: z.number().int().nonnegative().nullable(),
  oldest_sequence: z.number().int().nonnegative().nullable(),
  newest_sequence: z.number().int().nonnegative().nullable(),
  acknowledgement: z.number().int().nonnegative(),
  backlog: z.number().int().nonnegative().nullable(),
  free_internal_heap: z.number().int().nonnegative().nullable(),
  largest_internal_block: z.number().int().nonnegative().nullable(),
  last_reboot_reason: z.string().nullable(),
  last_command: z.object({ id: z.string(), type: z.string(), state: z.string(), progress_percent: z.number().min(0).max(100), expires_at: z.string().datetime({ offset: true }).optional(), result_code: z.string().nullable().optional(), result_evidence: z.record(z.string(), z.union([z.string(), z.number(), z.boolean(), z.null()])).optional() }).nullable(),
}).passthrough();

export const homeScopeSchema = z.object({
  id: z.string().length(36),
  name: z.string(),
}).strict();

export const homeScopesSchema = z.object({
  home_scopes: z.array(homeScopeSchema),
}).strict();

export const devicesSchema = z.object({
  home_scopes: z.array(homeScopeSchema).default([]),
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

export const firmwareDeploymentJobSchema = z.object({
  id: z.string(),
  device_id: z.string(),
  device_name: z.string(),
  previous_version: z.string().nullable(),
  current_version: z.string().nullable(),
  target_version: z.string(),
  target_build: z.number().int().positive(),
  state: z.enum(['staged', 'queued', 'downloading', 'rebooting', 'validating', 'succeeded', 'failed', 'rolled_back', 'timed_out', 'cancelled']),
  progress_percent: z.number().int().min(0).max(100),
  attempt: z.number().int().positive(),
  error_code: z.string().nullable(),
  error_message: z.string().nullable(),
  created_at: isoDate,
  updated_at: isoDate,
  completed_at: isoDate.nullable(),
  confirmation_heartbeat_at: isoDate.nullable(),
  reported_firmware_after_reboot: z.string().nullable(),
  retry_eligible: z.boolean(),
  cancel_eligible: z.boolean(),
}).strict();

export const firmwareDeploymentBatchSchema = z.object({
  id: z.string(),
  release_id: z.string(),
  target_version: z.string(),
  rollout: z.enum(['immediate', 'staged', 'retry', 'legacy']),
  state: z.enum(['queued', 'in_progress', 'partial', 'succeeded', 'failed', 'cancelled', 'expired']),
  targeted: z.number().int().positive(),
  succeeded: z.number().int().nonnegative(),
  failed: z.number().int().nonnegative(),
  pending: z.number().int().nonnegative(),
  created_at: isoDate,
  updated_at: isoDate,
  completed_at: isoDate.nullable(),
  jobs: z.array(firmwareDeploymentJobSchema),
}).strict();

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
  artifact_available: z.boolean(),
  release_notes: z.string().optional(),
  physical_certification: z.string().optional(),
  upload_status: z.enum(['uploaded', 'archived']).optional(),
  validation_status: z.enum(['ready', 'archived']).optional(),
  deployment_batches: z.array(firmwareDeploymentBatchSchema).optional().default([]),
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
export type RateCandidate = z.infer<typeof rateCandidateSchema>;
export type RateCandidates = z.infer<typeof rateCandidatesSchema>;
export type RateCandidateWorkflow = z.infer<typeof rateCandidateWorkflowSchema>;
export type RateSourceStatus = z.infer<typeof rateSourceStatusSchema>;
export type RateCheckResult = z.infer<typeof rateCheckResultSchema>;
export type Alert = z.infer<typeof alertSchema>;
export type DeviceDetail = z.infer<typeof deviceDetailSchema>;
export type HomeScope = z.infer<typeof homeScopeSchema>;
export type Command = z.infer<typeof commandSchema>;
export type SystemHealth = z.infer<typeof systemHealthSchema>;
export type BackupStatus = z.infer<typeof backupStatusSchema>;
export type HomeUtility = z.infer<typeof homeUtilitySchema>;
export type FirmwareRelease = z.infer<typeof firmwareReleaseSchema>;
export type FirmwareDeploymentBatch = z.infer<typeof firmwareDeploymentBatchSchema>;
export type FirmwareDeploymentJob = z.infer<typeof firmwareDeploymentJobSchema>;
export type Circuit = z.infer<typeof circuitSchema>;
