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
    permissions: allPermissions, mfa_enabled: true, enabled: true, manageable: true,
  },
};

export const home = {
  timezone: 'America/Los_Angeles',
  devices: [{
    id: 'device-main', friendly_name: 'Main Panel Sensor', state: 'live',
    measurement: { voltage_v: 122.6, current_a: 20.2, active_power_w: 2480, frequency_hz: 60.01, power_factor: 0.97, measured_at: '2026-08-13T17:32:10Z', pzem_status: 'ok' },
    heartbeat_at: '2026-08-13T17:32:10Z', last_committed_at: '2026-08-13T17:31:00Z', server_delivery_status: 'received', last_server_received_at: '2026-08-13T17:32:11Z', last_sensor_sampled_at: '2026-08-13T17:32:10Z', sensor_time_trusted: true, cumulative_energy_kwh: 392.8, telemetry_protocol: 'direct_https', firmware_version: 'v1.2.3', measurement_scope: 'energy_only', estimated_cost_per_hour: '0.42656',
  }],
  summaries: {
    today: { energy_kwh: 18.74, cost: '3.21', completeness: .99, missing_intervals: 2 },
    yesterday: { energy_kwh: 19.7, cost: '3.10', completeness: .995, missing_intervals: 1 },
    week: { energy_kwh: 118.2, cost: '18.67', completeness: .97, missing_intervals: 19 },
    last_week: { energy_kwh: 116.4, cost: '18.11', completeness: .98, missing_intervals: 8 },
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
  location: 'Main electrical panel', notes: null, display_order: 0, include_in_aggregate: true, show_on_dashboard: true, monitoring_enabled: true,
  pzem_variant: 'pzem004t-v4-classic-candidate', ct_rating_a: '100', measurement_scope: 'energy_only', heartbeat_at: '2026-08-13T17:32:10Z', wifi_rssi: -54, ip_address: '192.0.2.24', pzem_status: 'ok', telemetry_protocol: 'direct_https', server_delivery_status: 'received', last_server_received_at: '2026-08-13T17:32:11Z', last_sensor_sampled_at: '2026-08-13T17:32:10Z', sensor_time_trusted: true, cumulative_energy_kwh: 392.8,
  free_internal_heap: 208000, largest_internal_block: 98120,
  last_reboot_reason: 'software_update', last_command: { id: 'cmd-old', type: 'reboot', state: 'succeeded', progress_percent: 100 },
};

export const homeScopes = [{ id: '00000000-0000-0000-0000-000000000010', name: 'Home' }];

export const homeUtility = {
  home: { id: '00000000-0000-0000-0000-000000000010', name: 'Home', timezone: 'America/Los_Angeles' },
  utility: { id: '00000000-0000-0000-0000-000000000020', utility_name: 'Southern California Edison', timezone: 'America/Los_Angeles', billing_day: 12, cost_scope: 'energy_only', baseline_allocation_kwh: null, cca_provider: null },
  usage_source: 'authenticated PZEM-004T sensor intervals only',
};

export const circuits = { circuits: [{
  id: 'circuit-main-service', home_id: homeScopes[0]!.id, name: 'Main service', description: 'Whole-home service', purpose: 'whole_home_total', is_home_total: true,
  aggregate_mode: 'verified_sum', non_overlapping_confirmed: true, is_billing_source: true, device_ids: ['device-main'], created_at: '2026-08-13T16:00:00Z', updated_at: '2026-08-13T16:00:00Z',
}] };

export const firmwareReleases = {
  releases: [{
    schema: 'pm-ota-manifest/1.0.0', release_id: '00000000-0000-0000-0000-000000000030', semantic_version: '1.2.4', build_number: 851,
    project_name: 'power-monitor-sensor-headless', target_chip: 'esp32s3', board_profile: 'esp32-s3-devkitc-n16r8-reference/1', minimum_boot_version: 1, minimum_protocol: 'pm-protocol/1.0.0', minimum_config_version: 1,
    image_size: 1_048_576, sha256: 'b'.repeat(64), firmware_build_id: 'c'.repeat(64), candidate: true, artifact_available: true, upload_status: 'uploaded', validation_status: 'ready', release_notes: 'Candidate release for staged validation.', physical_certification: 'pending',
    release_state: 'available', archived_at: null, deleted_at: null, artifact_deleted_at: null, rollback_pinned: false, deployment_count: 2, active_deployment_count: 0, sensor_reported_count: 0, deploy_eligible: true, archive_eligible: true, restore_eligible: false, delete_eligibility: { eligible: true, protection_reasons: [] },
    deployment_batches: [{
      id: '00000000-0000-0000-0000-000000000031', release_id: '00000000-0000-0000-0000-000000000030', target_version: '1.2.4', rollout: 'staged', state: 'partial', targeted: 2, succeeded: 1, failed: 1, pending: 0,
      result_state: 'partial', deployment_state: 'partial', archived_at: null, deleted_at: null, troubleshooting_hold: false, archive_eligible: true, restore_eligible: false, delete_eligibility: { eligible: false, protection_reasons: ['archive_required_before_deletion'] }, created_at: '2026-08-13T17:00:00Z', updated_at: '2026-08-13T17:10:00Z', completed_at: '2026-08-13T17:10:00Z',
      jobs: [
        { id: '00000000-0000-0000-0000-000000000032', device_id: 'device-main', device_name: 'Indoor-AC', previous_version: '1.2.3', current_version: '1.2.4', target_version: '1.2.4', target_build: 851, target_firmware_build_id: 'c'.repeat(64), state: 'succeeded', attempt_state: 'updated', progress_percent: 100, attempt: 1, error_code: null, error_message: null, created_at: '2026-08-13T17:00:00Z', updated_at: '2026-08-13T17:08:00Z', completed_at: '2026-08-13T17:08:00Z', confirmation_heartbeat_at: '2026-08-13T17:08:00Z', reported_firmware_after_reboot: '1.2.4', reported_firmware_build_id_after_reboot: 'c'.repeat(64), retry_eligible: false, cancel_eligible: false },
        { id: '00000000-0000-0000-0000-000000000033', device_id: 'device-outdoor', device_name: 'Outdoor-AC', previous_version: '1.2.3', current_version: '1.2.3', target_version: '1.2.4', target_build: 851, target_firmware_build_id: 'c'.repeat(64), state: 'failed', attempt_state: 'failed', progress_percent: 0, attempt: 1, error_code: 'OTA_VERSION_NOT_CONFIRMED', error_message: 'Outdoor-AC reconnected on 1.2.3 instead of 1.2.4', created_at: '2026-08-13T17:00:00Z', updated_at: '2026-08-13T17:10:00Z', completed_at: '2026-08-13T17:10:00Z', confirmation_heartbeat_at: null, reported_firmware_after_reboot: '1.2.3', reported_firmware_build_id_after_reboot: 'd'.repeat(64), retry_eligible: true, cancel_eligible: false },
      ],
    }],
  }],
  reconciliation: {
    strategy: 'explicit_state_and_artifact_evidence',
    inconsistent_release_ids: [],
    silent_deletion_performed: false,
    artifact_quarantines: {
      apply: false,
      restored_release_ids: [],
      purged_release_ids: [],
      purged_unknown_upload_release_ids: [],
      promoted_upload_release_ids: [],
      collision_release_ids: [],
      corrupt_artifact_release_ids: [],
      unknown_release_ids: [],
      orphan_final_release_ids: [],
      orphan_temp_release_ids: [],
      deferred_recovery_release_ids: [],
      recovery_grace_seconds: 30,
      unsafe_entries: [],
      attention_required: false,
    },
  },
};

export const firmwareLifecycleSettings = {
  deployment_retention_days: 365,
  retention_policy: '365_days',
  automatic_cleanup_scope: 'archived_terminal_deployment_tombstones_only',
  firmware_artifacts_affected: false,
  active_deployments_affected: false,
  updated_at: '2026-08-13T16:00:00Z',
};

const historyTimestamps = ['2026-08-13T10:00:00Z', '2026-08-13T12:00:00Z', '2026-08-13T14:00:00Z', '2026-08-13T16:00:00Z', '2026-08-13T18:00:00Z', '2026-08-13T20:00:00Z', '2026-08-13T22:00:00Z'];
export const history = {
  points: historyTimestamps.map((timestamp, index) => ({ timestamp, value: index === 3 ? null : [0, 2.1, 3.4, 0, 4.55, 2.7, 2.48][index], cost: index === 3 ? null : String(index * .11), quality: index === 3 ? .25 : 1 })),
  energy_kwh: 18.74, cost: '3.21', completeness: .93,
  missing_ranges: [{ start: '2026-08-13T15:30:00Z', end: '2026-08-13T16:30:00Z' }],
  connection_gaps: [{ device_id: 'device-main', start_utc: '2026-08-13T15:30:00Z', end_utc: '2026-08-13T16:30:00Z', recovered_energy_kwh: '0.42', status: 'recovered', crosses_billing_cycle: false }],
  recovered_gap_energy_kwh: '0.42',
  resolution_seconds: 7200, timezone: 'UTC', usage_source: 'authenticated PZEM-004T sensor intervals only', scope: { device_ids: ['device-main'], aggregate: false },
};

export const dailyHistory = {
  ...history,
  points: ['2026-08-07', '2026-08-08', '2026-08-09', '2026-08-10', '2026-08-11', '2026-08-12', '2026-08-13'].map((date, index) => ({ timestamp: `${date}T12:00:00Z`, value: [17.1, 20.6, 19.8, 17.4, 20.5, 19.7, 18.74][index], cost: String(2.8 + index * .1), quality: 1 })),
  missing_ranges: [], resolution_seconds: 86400,
};

export const alerts = {
  alerts: [
    { id: 'alert-delivery', type: 'sensor_delivery_delayed', severity: 'warning', state: 'open', opened_at: '2026-08-13T17:31:50Z', evidence: { last_received_at: '2026-08-13T17:31:40Z' } },
    { id: 'alert-rate', type: 'rate_source_changed', severity: 'info', state: 'acknowledged', opened_at: '2026-08-13T16:00:00Z', evidence: { review_required: true } },
  ],
};

export const billing = {
  accounts: [{ utility_account_id: '00000000-0000-0000-0000-000000000020', plan_name: 'SCE Domestic', rate_version_id: 'rate-version-2026-08', effective_start: '2026-08-01T07:00:00Z', cost_scope: 'full_account', baseline_credit_included: false, fixed_charges_included: true, cca_or_direct_access: null,
    home_total_branch: { id: 'circuit-main-service', name: 'Main service', device_ids: ['device-main'] },
    current_rate_plan: { rate_plan_id: 'rate-domestic', rate_plan_version_id: 'rate-version-2026-08', name: 'SCE Domestic', utility_name: 'Southern California Edison', rate_class: 'residential_tiered', effective_start: '2026-08-01T07:00:00Z', currently_used: true, tier_1_price_per_kwh: '0.30863', tier_2_price_per_kwh: '0.40962', daily_service_charge: '0.769', daily_baseline_allowance_kwh: '19.3', generation_service: 'SCE' },
    current_billing_cycle: { start_utc: '2026-08-01T07:00:00Z', end_utc: '2026-09-01T07:00:00Z', service_branch_id: 'circuit-main-service', service_branch_name: 'Main service', saved_usage_kwh: '0.17', reading_coverage: '0.004', tier_state: 'not_confirmed', tier_confirmation_rule: 'requires_100_percent_reading_coverage', tier_1_allowance_kwh: '598.3', tier_1_remaining_kwh: null, amount_above_tier_1_kwh: null, connection_gap_energy_kwh: '0', unresolved_energy_kwh: null, tier_1_usage_kwh: null, tier_2_usage_kwh: null, tier_1_rate_per_kwh: '0.30863', tier_2_rate_per_kwh: '0.40962', tier_1_cost: null, tier_2_cost: null, service_charge: null, cost_to_date: null, estimated_energy_charges: null, estimated_fixed_charges: null, estimated_total: null, projection: { status: 'not_enough_data', confidence_reasons: ['At least 24 hours of reliable readings are required.'] } },
  }],
  usage_source: 'authenticated PZEM-004T sensor intervals only', rate_import_notice: 'PDFs create reviewed reusable rate-plan drafts only.',
};

export const rateCandidate = {
  id: '00000000-0000-0000-0000-000000000060',
  state: 'review_required',
  created_at: '2026-08-13T16:00:00Z',
  reviewed_at: null,
  source: {
    id: '00000000-0000-0000-0000-000000000061',
    name: 'Southern California Edison TOU rate plans',
    url: 'https://www.sce.com/save-money/rates-financing/residential-rate-plans/time-of-use-plans',
    revision_id: '00000000-0000-0000-0000-000000000062',
    artifact_sha256: 'd'.repeat(64),
    retrieved_at: '2026-08-13T16:00:00Z',
    parser_version: 'sce-tou-public-v1',
  },
  normalized_rates: {
    schema: 'sce-rate-candidate/1.0.0',
    utility_name: 'Southern California Edison',
    timezone: 'America/Los_Angeles',
    currency: 'USD',
    season_definitions: { summer: { start_month: 6, end_month: 9 }, winter: { start_month: 10, end_month: 5 } },
    holiday_rule: 'weekend_rates',
    effective_start: null,
    effective_end: null,
    effective_date_confirmation_required: true,
    plans: [{
      rate_plan_name: 'TOU-D-4-9PM', rate_class: 'residential', pricing_model: 'time_of_use_plus_baseline_credit',
      daily_fixed_charge: '0.79000000', monthly_fixed_charge: '0.00000000', baseline_credit_per_kwh: '0.10000000',
      rate_components: 'sce_delivery_and_generation_combined',
      periods: [{ season: 'all', day_type: 'all', name: 'all_day', start_minute: 0, end_minute: 1440, price_per_kwh: '0.12345678', currency: 'USD', unit: 'kWh', tier_min_kwh: null, tier_max_kwh: null }],
    }],
  },
  validation_evidence: { parser_version: 'sce-tou-public-v1', schema: 'sce-rate-candidate/1.0.0', plan_count: 1, period_count: 1, seasons: ['summer', 'winter'], day_types: ['weekday', 'weekend', 'holiday'], coverage: 'complete', price_unit: 'USD/kWh', effective_date: 'administrator_confirmation_required' },
  diff: { change_count: 1 },
  manual_approval_required: true,
  workflow: { state: 'review_required' },
};

export const rateCandidates = { home_id: homeScopes[0]!.id, candidates: [rateCandidate] };

export const rateSourceStatus = {
  home_id: homeScopes[0]!.id,
  scheduled: { state: 'enabled', source_id: rateCandidate.source.id, source_name: rateCandidate.source.name, source_url: rateCandidate.source.url, check_interval_hours: 24, next_check_at: '2026-08-14T16:00:00Z' },
  last_run: { id: 'rate-run-old', source_id: rateCandidate.source.id, source_name: rateCandidate.source.name, source_type: 'official_https', source_url: rateCandidate.source.url, state: 'review_required', event_code: 'RATE_SOURCE_CHANGED', started_at: '2026-08-13T15:59:59Z', completed_at: '2026-08-13T16:00:00Z', revision_id: rateCandidate.source.revision_id, error_code: null, initiator: 'user' },
  last_success: { id: 'rate-run-old', source_id: rateCandidate.source.id, source_name: rateCandidate.source.name, source_type: 'official_https', source_url: rateCandidate.source.url, state: 'review_required', event_code: 'RATE_SOURCE_CHANGED', started_at: '2026-08-13T15:59:59Z', completed_at: '2026-08-13T16:00:00Z', revision_id: rateCandidate.source.revision_id, error_code: null, initiator: 'user' },
  last_failure: null,
  active: { state: 'active', utility_account_id: billing.accounts[0]!.utility_account_id, assignment_id: '00000000-0000-0000-0000-000000000063', rate_plan_version_id: 'rate-version-2026-08', plan_name: 'SCE TOU-D-4-9PM', effective_start: '2026-08-01T07:00:00Z', effective_end: null, provenance: { source_artifact_sha256: 'c'.repeat(64), origin: 'official_https', source_name: rateCandidate.source.name, source_url: rateCandidate.source.url, source_revision_id: rateCandidate.source.revision_id, candidate_id: rateCandidate.id, review_id: '00000000-0000-0000-0000-000000000064' } },
  last_known_good: { state: 'available', candidate_id: rateCandidate.id, source_revision_id: rateCandidate.source.revision_id, source_artifact_sha256: rateCandidate.source.artifact_sha256, retrieved_at: rateCandidate.source.retrieved_at, source_name: rateCandidate.source.name, source_type: 'official_https', source_url: rateCandidate.source.url, active_source_match: false },
};

export const sceRateCatalog = {
  home_id: homeScopes[0]!.id,
  summary: {
    plans_discovered: 1,
    plans_parsed: 1,
    plans_requiring_parser_updates: 0,
    plans_explicitly_excluded: 0,
    plans_silently_omitted: null,
    last_successful_official_check: '2026-08-13T16:00:00Z',
    current_catalog_effective_date: null,
    open_plans: 1,
    eligibility_required_plans: 0,
    existing_customer_only_plans: 0,
  },
  plans: [{
    id: '00000000-0000-0000-0000-000000000080',
    canonical_name: 'DOMESTIC',
    public_plan_name: 'SCE Domestic',
    official_schedule_code: 'D',
    plan_type: 'seasonal_tiered',
    enrollment_status: 'open_or_eligibility_required',
    eligibility: [],
    eligibility_requirements: [],
    description: 'Standard residential tiered service.',
    effective_start: null,
    effective_end: null,
    timezone: 'America/Los_Angeles',
    currency: 'USD',
    energy_unit: 'kWh',
    source_version: 'f'.repeat(64),
    holiday_treatment: 'not_applicable',
    season_definitions: {},
    seasons: ['summer'],
    day_types: ['all_days'],
    period_count: 2,
    periods: [
      { season: 'summer', day_type: 'all_days', period_name: 'tier_1', start_minute: null, end_minute: null, local_start_time: null, local_end_time: null, price_per_kwh: '0.30863', currency: 'USD', energy_unit: 'kWh', rate_components: [{ component: 'delivery', amount_per_kwh: '0.17862' }, { component: 'generation', amount_per_kwh: '0.11761' }, { component: 'wildfire_fund', amount_per_kwh: '0.00591' }, { component: 'fixed_recovery', amount_per_kwh: '0.00619' }, { component: 'state_tax', amount_per_kwh: '0.00030' }], tier: { lower_bound_kwh: null, upper_bound_kwh: '579.0', boundary_inclusive: true, threshold_basis: 'daily_baseline_allowance', threshold_value: '19.3' }, source_label: 'Summer Tier 1' },
      { season: 'summer', day_type: 'all_days', period_name: 'tier_2', start_minute: null, end_minute: null, local_start_time: null, local_end_time: null, price_per_kwh: '0.40962', currency: 'USD', energy_unit: 'kWh', rate_components: [{ component: 'delivery', amount_per_kwh: '0.27961' }, { component: 'generation', amount_per_kwh: '0.11761' }, { component: 'wildfire_fund', amount_per_kwh: '0.00591' }, { component: 'fixed_recovery', amount_per_kwh: '0.00619' }, { component: 'state_tax', amount_per_kwh: '0.00030' }], tier: { lower_bound_kwh: '579.0', upper_bound_kwh: null, boundary_inclusive: false, threshold_basis: 'daily_baseline_allowance', threshold_value: '19.3' }, source_label: 'Summer Tier 2' },
    ],
    schedule: [],
    daily_fixed_charge: '0.76900',
    monthly_fixed_charge: '0',
    baseline_credit_per_kwh: '0',
    tier_threshold_basis: 'daily_baseline_allowance',
    rate_precision: 'approved_tariff_exact',
    exact_rates_verified: true,
    rate_component_scope: 'sce_delivery_and_generation_itemized',
    baseline_credit_scope: 'not_applicable',
    verification_state: 'parsed',
    latest_discovery_state: 'parsed',
    latest_discovery_revision_id: '00000000-0000-0000-0000-000000000081',
    last_known_good_retained: false,
    exclusion_reason: null,
    currently_used: true,
    source: {
      level: 3,
      name: 'Southern California Edison residential tariffs',
      url: 'https://www.sce.com/rates',
      revision_id: '00000000-0000-0000-0000-000000000081',
      artifact_sha256: 'f'.repeat(64),
      retrieved_at: '2026-08-13T16:00:00Z',
      parser_version: 'sce-public-catalog-v1',
    },
  }],
  source_policy: 'official_public_sce_only',
  inventory_scope: 'bounded_official_multi_document_crawl',
  catalog_completeness: 'crawl_incomplete',
  catalog_ready: false,
  completeness_reason: 'crawl_not_yet_complete',
  live_source_access_performed: false,
};

export const systemHealth = {
  generated_at: '2026-08-13T17:32:00Z', version: '0.1.0-rc.27', protocol: 'pm-protocol/1.0.0', database: 'reachable',
  sensors: [{ device_id: 'device-main', state: 'online', heartbeat_age_seconds: 5, pzem_status: 'ok', telemetry_protocol: 'direct_https', server_delivery_status: 'received', last_server_received_at: '2026-08-13T17:32:11Z', last_sensor_sampled_at: '2026-08-13T17:32:10Z', sensor_time_trusted: true }],
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
  const pathname = new URL(path, 'http://frontend.test').pathname;
  if (path.endsWith('/auth/bootstrap/status')) return { status: 200, body: { required: false } };
  if (path.endsWith('/auth/me')) return { status: 200, body: session.user };
  if (path.endsWith('/auth/login') || path.endsWith('/auth/bootstrap')) return { status: 200, body: { user: session.user } };
  if (pathname.endsWith('/home-scopes')) return { status: 200, body: { home_scopes: homeScopes } };
  if (pathname.endsWith('/settings/home-utility')) return { status: 200, body: homeUtility };
  if (pathname.endsWith('/settings/telemetry')) return { status: 200, body: { config_version: 1, telemetry_interval_seconds: 5, history_interval_seconds: 60, retention_days: 365 } };
  if (pathname.endsWith('/home')) return { status: 200, body: home };
  if (path.includes('/history/export')) return { status: 200, body: 'timestamp,value\n2026-08-13T10:00:00Z,0\n', contentType: 'text/csv' };
  if (path.includes('/history')) return { status: 200, body: path.includes('resolution_seconds=86400') ? dailyHistory : history };
  if (path.includes('/devices') && path.endsWith('/commands') && method === 'POST') return { status: 202, body: { command: { id: 'cmd-new', type: 'sync_now', state: 'queued' }, confirmation_token: null } };
  if (path.includes('/devices/') && path.endsWith('/credentials/rotate') && method === 'POST') return { status: 202, body: { rotation: { rotation_id: '00000000-0000-0000-0000-000000000050', credential_fingerprint: 'b'.repeat(64), state: 'pending', overlap_expires_at: '2026-08-13T17:42:10Z', prepare_command_id: '00000000-0000-0000-0000-000000000051', commit_command_id: null, cancel_command_id: null } } };
  if (path.includes('/credentials/rotations/') && path.endsWith('/cancel') && method === 'POST') return { status: 202, body: { rotation: { rotation_id: '00000000-0000-0000-0000-000000000050', credential_fingerprint: 'b'.repeat(64), state: 'pending', overlap_expires_at: '2026-08-13T17:42:10Z', prepare_command_id: '00000000-0000-0000-0000-000000000051', commit_command_id: null, cancel_command_id: '00000000-0000-0000-0000-000000000052' } } };
  if (path.endsWith('/enrollment-tokens') && method === 'POST') return { status: 201, body: { token: 'single-use-enrollment-token-value-000000000000', expires_at: '2026-08-13T17:47:00Z' } };
  if (path.endsWith('/circuits/verified-aggregates') && method === 'POST') return { status: 201, body: { id: '00000000-0000-0000-0000-000000000040', name: 'Verified whole home', device_ids: ['device-main', 'device-secondary'] } };
  if (pathname.endsWith('/circuits') && method === 'POST') return { status: 201, body: circuits.circuits[0] };
  if (pathname.includes('/circuits/') && method === 'PATCH') return { status: 200, body: circuits.circuits[0] };
  if (pathname.includes('/circuits/') && method === 'DELETE') return { status: 204 };
  if (pathname.endsWith('/circuits')) return { status: 200, body: circuits };
  if (pathname.endsWith('/devices')) return { status: 200, body: { home_scopes: homeScopes, devices: [device] } };
  if (path.includes('/devices/') && path.endsWith('/revoke') && method === 'POST') return { status: 204 };
  if (path.includes('/devices/') && method === 'PATCH') return { status: 200, body: { id: device.id, friendly_name: device.friendly_name, measurement_scope: 'energy_only' } };
  if (path.endsWith('/alerts')) return { status: 200, body: alerts };
  if (path.endsWith('/acknowledge')) return { status: 200, body: { id: 'alert-delivery', state: 'acknowledged' } };
  if (path.endsWith('/silence')) return { status: 200, body: { id: 'alert-delivery', silenced_until: '2026-08-14T17:32:00Z' } };
  if (pathname.endsWith('/billing')) return { status: 200, body: billing };
  if (pathname.endsWith('/rate-sources/catalog')) return { status: 200, body: sceRateCatalog };
  if (pathname.endsWith('/bill-rate-imports')) return { status: 200, body: { extractions: [] } };
  if (pathname.endsWith('/rate-sources/status')) return { status: 200, body: rateSourceStatus };
  if (pathname.endsWith('/rate-sources/candidates') && method === 'GET') return { status: 200, body: rateCandidates };
  if (pathname.endsWith('/rate-sources/runs')) return { status: 200, body: { home_id: homeScopes[0]!.id, runs: [] } };
  if (pathname.endsWith('/rate-sources/manual-candidates') && method === 'POST') return { status: 201, body: { home_id: homeScopes[0]!.id, created: true, candidate_id: rateCandidate.id, revision_id: rateCandidate.source.revision_id, source_id: rateCandidate.source.id, run_id: 'rate-run-manual', state: 'review_required', canonical_input_sha256: rateCandidate.source.artifact_sha256, network_fetch_performed: false } };
  if (pathname.endsWith('/review') && pathname.includes('/rate-sources/candidates/') && method === 'POST') return { status: 200, body: { home_id: homeScopes[0]!.id, candidate_id: rateCandidate.id, workflow: { id: 'review-1', state: 'reviewed', selected_plan_name: 'TOU-D-4-9PM', effective_start: '2026-08-01T07:00:00Z', effective_end: null, reviewed_at: '2026-08-13T17:00:00Z', published_at: null, activated_at: null, rate_plan_version_id: null, utility_account_id: null } } };
  if (pathname.endsWith('/reject') && pathname.includes('/rate-sources/candidates/') && method === 'POST') return { status: 200, body: { home_id: homeScopes[0]!.id, candidate_id: rateCandidate.id, workflow: { id: 'review-1', state: 'rejected', selected_plan_name: null, effective_start: null, effective_end: null, reviewed_at: '2026-08-13T17:00:00Z', published_at: null, activated_at: null, rate_plan_version_id: null, utility_account_id: null } } };
  if (pathname.includes('/rate-sources/candidates/') && method === 'DELETE') return { status: 204 };
  if (pathname.includes('/bill-rate-imports/') && method === 'DELETE') return { status: 204 };
  if (pathname.endsWith('/publish') && pathname.includes('/rate-sources/candidates/') && method === 'POST') return { status: 201, body: { home_id: homeScopes[0]!.id, candidate_id: rateCandidate.id, workflow: { id: 'review-1', state: 'published', selected_plan_name: 'TOU-D-4-9PM', effective_start: '2026-08-01T07:00:00Z', effective_end: null, reviewed_at: '2026-08-13T17:00:00Z', published_at: '2026-08-13T17:01:00Z', activated_at: null, rate_plan_version_id: 'rate-version-new', utility_account_id: null }, rate_plan_version: { id: 'rate-version-new', plan_id: 'rate-plan-1', plan_name: 'TOU-D-4-9PM', version: 2, effective_start: '2026-08-01T07:00:00Z', effective_end: null, source_artifact_sha256: rateCandidate.source.artifact_sha256, state: 'published' } } };
  if (pathname.endsWith('/activate') && pathname.includes('/rate-sources/candidates/') && method === 'POST') return { status: 201, body: { home_id: homeScopes[0]!.id, candidate_id: rateCandidate.id, workflow: { id: 'review-1', state: 'activated', selected_plan_name: 'TOU-D-4-9PM', effective_start: '2026-08-01T07:00:00Z', effective_end: null, reviewed_at: '2026-08-13T17:00:00Z', published_at: '2026-08-13T17:01:00Z', activated_at: '2026-08-13T17:02:00Z', rate_plan_version_id: 'rate-version-new', utility_account_id: billing.accounts[0]!.utility_account_id }, assignment: { id: 'assignment-new', utility_account_id: billing.accounts[0]!.utility_account_id, rate_plan_version_id: 'rate-version-new', effective_start: '2026-08-01T07:00:00Z', effective_end: null } } };
  if (pathname.endsWith('/rate-sources/check-now')) return { status: 202, body: { run_id: 'rate-run-1', state: 'review_required', event_code: 'RATE_SOURCE_CHANGED', revision_id: rateCandidate.source.revision_id, candidate_id: rateCandidate.id, error_code: null } };
  if (path.endsWith('/auth/profile') && method === 'PATCH') return { status: 200, body: { id: session.user.id, email: session.user.email, display_name: session.user.display_name, session_revoked: false } };
  if (path.endsWith('/auth/profile')) return { status: 200, body: { id: session.user.id, email: session.user.email, display_name: session.user.display_name, enabled: true, roles: session.user.roles, created_at: '2026-08-01T00:00:00Z', preferences: {} } };
  if (path.endsWith('/auth/preferences')) return { status: 200, body: { preferences: { dashboard_range: 'today', history_range: 'week', refresh_seconds: 60, power_unit: 'auto', energy_unit: 'auto', date_format: 'us', time_format: '12h', decimal_precision: 2, density: 'comfortable', dashboard_cards: ['live_power', 'energy', 'cost', 'completeness', 'alerts'] } } };
  if (path.endsWith('/auth/change-password')) return { status: 204 };
  if (path.endsWith('/users') && method === 'POST') return { status: 201, body: { id: 'user-new', email: 'new@example.test', display_name: 'New User' } };
  if (path.endsWith('/users')) return { status: 200, body: { users: [session.user] } };
  if (path.endsWith('/roles')) return { status: 200, body: { roles: [{ id: 'role-owner', name: 'Owner', permissions: allPermissions, built_in: true }], available_permissions: allPermissions } };
  if (path.includes('/users/') && method === 'PATCH') return { status: 200, body: { id: session.user.id, enabled: true, display_name: session.user.display_name } };
  if (path.includes('/users/') && path.endsWith('/reset-password')) return { status: 204 };
  if (path.includes('/users/') && path.endsWith('/restore')) return { status: 200, body: { id: session.user.id, enabled: true } };
  if (path.includes('/users/') && method === 'DELETE') return { status: 204 };
  if (path.endsWith('/firmware/releases') && method === 'POST') return { status: 201, body: { release: firmwareReleases.releases[0], manifest_signature: 'fixture-signature', physical_certification: 'pending' } };
  if (pathname.includes('/firmware/releases/') && pathname.endsWith('/archive') && method === 'POST') return { status: 200, body: { ...firmwareReleases.releases[0], release_state: 'archived', archive_eligible: false, restore_eligible: true, deploy_eligible: false } };
  if (pathname.includes('/firmware/releases/') && pathname.endsWith('/restore') && method === 'POST') return { status: 200, body: { ...firmwareReleases.releases[0], release_state: 'available', archive_eligible: true, restore_eligible: false, deploy_eligible: true } };
  if (pathname.includes('/firmware/releases/') && pathname.endsWith('/make-current') && method === 'POST') return { status: 200, body: { ...firmwareReleases.releases[0], release_state: 'current', rollback_pinned: false } };
  if (pathname.includes('/firmware/releases/') && pathname.endsWith('/rollback-pin') && method === 'PATCH') return { status: 200, body: { ...firmwareReleases.releases[0], rollback_pinned: true } };
  if (pathname.includes('/firmware/releases/') && pathname.endsWith('/delete-permanently') && method === 'POST') return { status: 204 };
  if (pathname.endsWith('/firmware/lifecycle-settings') && method === 'PATCH') return { status: 200, body: firmwareLifecycleSettings };
  if (pathname.endsWith('/firmware/lifecycle-settings')) return { status: 200, body: firmwareLifecycleSettings };
  if (pathname.includes('/firmware/deployment-batches/') && pathname.endsWith('/archive') && method === 'POST') return { status: 200, body: { ...firmwareReleases.releases[0]!.deployment_batches[0], state: 'archived', deployment_state: 'archived', archived_at: '2026-08-13T18:00:00Z', archive_eligible: false, restore_eligible: true, delete_eligibility: { eligible: true, protection_reasons: [] } } };
  if (pathname.includes('/firmware/deployment-batches/') && pathname.endsWith('/restore') && method === 'POST') return { status: 200, body: firmwareReleases.releases[0]!.deployment_batches[0] };
  if (pathname.includes('/firmware/deployment-batches/') && pathname.endsWith('/delete-permanently') && method === 'POST') return { status: 204 };
  if (path.includes('/firmware/releases/') && method === 'DELETE') return { status: 204 };
  if (pathname.endsWith('/firmware/releases')) return { status: 200, body: firmwareReleases };
  if (path.includes('/firmware/releases/') && path.endsWith('/deploy')) return { status: 202, body: { batch_id: 'deployment-batch-1', batch_state: 'in_progress', deployments: [{ id: 'deployment-1', device_id: device.id, state: 'queued' }] } };
  if (path.includes('/firmware/deployment-batches/') && path.endsWith('/retry')) return { status: 202, body: { batch_id: 'deployment-batch-retry', batch_state: 'in_progress', deployments: [{ id: 'deployment-retry', device_id: 'device-outdoor', state: 'queued' }] } };
  if (path.includes('/firmware/deployment-batches/') && path.endsWith('/cancel')) return { status: 200, body: { batch_id: 'deployment-batch-1', state: 'cancelled' } };
  if (path.endsWith('/system/health')) return { status: 200, body: systemHealth };
  if (path.endsWith('/backups/status')) return { status: 200, body: backupStatus };
  if (path.endsWith('/diagnostics/bundle')) return { status: 200, body: 'redacted', contentType: 'application/zip' };
  if (path.endsWith('/auth/logout')) return { status: 204 };
  return { status: 404, body: { type: 'about:blank', title: 'Not found', status: 404, detail: path } };
}
