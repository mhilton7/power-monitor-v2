import type { Page, Route } from '@playwright/test';
import { alerts, apiResponse, backupStatus, billing, circuits, dailyHistory, device, firmwareReleases, history, home, homeScopes, homeUtility, rateCandidate, rateCandidates, rateSourceStatus, session, systemHealth } from '../fixtures';

interface MockWorkflow { state: string; [key: string]: unknown }
type FixtureSource = typeof rateCandidate.source;
type FixtureNormalized = typeof rateCandidate.normalized_rates;
type FixturePlan = FixtureNormalized['plans'][number];
type MockPlan = Omit<FixturePlan, 'rate_components' | 'periods'> & { rate_components: string; periods: Array<Record<string, unknown>> };
type MockNormalized = Omit<FixtureNormalized, 'holiday_rule' | 'effective_start' | 'effective_end' | 'plans'> & { holiday_rule: string; effective_start: string | null; effective_end: string | null; plans: MockPlan[] };
type MockRateCandidate = Omit<typeof rateCandidate, 'workflow' | 'source' | 'normalized_rates' | 'validation_evidence'> & {
  workflow: MockWorkflow;
  source: Omit<FixtureSource, 'url'> & { url: string | null };
  normalized_rates: MockNormalized;
  validation_evidence: Record<string, unknown>;
};

interface MockOptions {
  sessionExpired?: boolean;
  forbiddenCommands?: boolean;
  homeOverride?: Record<string, unknown>;
  homeScopesOverride?: Array<{ id: string; name: string }>;
  devicesOverride?: Array<typeof device>;
  sessionOverride?: typeof session.user;
  homeById?: Record<string, Record<string, unknown>>;
  homeUtilityById?: Record<string, typeof homeUtility>;
  billingById?: Record<string, typeof billing>;
  devicesById?: Record<string, { home_scopes: Array<{ id: string; name: string }>; devices: Array<typeof device> }>;
  delayedHomeId?: string;
  delayMs?: number;
  rateCheckOverride?: { run_id: string; state: 'review_required' | 'unchanged' | 'failed'; event_code: string; revision_id: string | null; candidate_id: string | null; error_code: string | null };
  rateCandidatesById?: Record<string, typeof rateCandidates>;
  rateStatusById?: Record<string, typeof rateSourceStatus>;
  rateRejectFailureOnce?: boolean;
  rateRejectDelayMs?: number;
}

export async function mockApi(page: Page, options: MockOptions = {}) {
  await page.clock.setFixedTime(new Date('2026-08-13T17:32:15Z'));
  const commandTypes: string[] = [];
  let formatPrepared = false;
  const candidateStore = new Map<string, MockRateCandidate[]>();
  let rejectAttempts = 0;
  const candidatesFor = (homeId: string) => {
    const existing = candidateStore.get(homeId);
    if (existing) return existing;
    const initial = structuredClone(options.rateCandidatesById?.[homeId]?.candidates ?? rateCandidates.candidates) as MockRateCandidate[];
    candidateStore.set(homeId, initial);
    return initial;
  };

  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    const homeId = url.searchParams.get('home_id') ?? '';

    if (homeId && homeId === options.delayedHomeId && options.delayMs) await new Promise((resolve) => setTimeout(resolve, options.delayMs));

    if (options.sessionExpired && path.endsWith('/auth/me')) {
      await route.fulfill({ status: 401, contentType: 'application/problem+json', body: JSON.stringify({ type: 'about:blank', title: 'Unauthorized', status: 401, detail: 'Session expired.' }) });
      return;
    }
    if (path.endsWith('/auth/bootstrap/status')) { await json(route, { required: false }); return; }
    if (path.endsWith('/auth/me')) { await json(route, options.sessionOverride ?? session.user); return; }
    if (path.endsWith('/auth/login') || path.endsWith('/auth/bootstrap')) { await json(route, { user: options.sessionOverride ?? session.user }); return; }
    if (path.endsWith('/auth/logout')) { await route.fulfill({ status: 204 }); return; }
    if (path.endsWith('/home-scopes')) { await json(route, { home_scopes: options.homeScopesOverride ?? homeScopes }); return; }
    if (path.endsWith('/settings/home-utility')) {
      if (options.sessionOverride && !options.sessionOverride.permissions.includes('billing.view')) { await problem(route, 403, 'Billing permission is required.'); return; }
      await json(route, options.homeUtilityById?.[homeId] ?? homeUtility);
      return;
    }
    if (path.endsWith('/home')) { await json(route, options.homeById?.[homeId] ?? options.homeOverride ?? home); return; }
    if (path.endsWith('/enrollment-tokens') && method === 'POST') { await json(route, { token: 'single-use-enrollment-token-value-000000000000', expires_at: '2026-08-13T17:47:00Z' }, 201); return; }
    if (path.endsWith('/credentials/rotate') && path.includes('/devices/') && method === 'POST') { await json(route, { rotation: { rotation_id: '00000000-0000-0000-0000-000000000050', credential_fingerprint: 'b'.repeat(64), state: 'pending', overlap_expires_at: '2026-08-13T17:42:10Z', prepare_command_id: '00000000-0000-0000-0000-000000000051', commit_command_id: null, cancel_command_id: null } }, 202); return; }
    if (path.endsWith('/cancel') && path.includes('/credentials/rotations/') && method === 'POST') { await json(route, { rotation: { rotation_id: '00000000-0000-0000-0000-000000000050', credential_fingerprint: 'b'.repeat(64), state: 'pending', overlap_expires_at: '2026-08-13T17:42:10Z', prepare_command_id: '00000000-0000-0000-0000-000000000051', commit_command_id: null, cancel_command_id: '00000000-0000-0000-0000-000000000052' } }, 202); return; }
    if (path.endsWith('/circuits/verified-aggregates') && method === 'POST') { await json(route, { id: '00000000-0000-0000-0000-000000000040', name: 'Verified whole home', device_ids: ['device-main', 'device-secondary'] }, 201); return; }
    if (path.endsWith('/circuits')) { await json(route, circuits); return; }
    if (path.endsWith('/devices')) { await json(route, options.devicesById?.[homeId] ?? { home_scopes: options.homeScopesOverride ?? homeScopes, devices: options.devicesOverride ?? [{ ...device, ...(formatPrepared ? { last_command: { id: '00000000-0000-0000-0000-000000000001', type: 'format_storage_prepare', state: 'succeeded', progress_percent: 100, result_code: 'ok', result_evidence: { prepare_command_id: '00000000-0000-0000-0000-000000000001', acknowledged_records_lost: 42, unacknowledged_records_lost: 5, ready: true } } } : {}) }] }); return; }
    if (path.endsWith('/revoke') && path.includes('/devices/') && method === 'POST') { await route.fulfill({ status: 204 }); return; }
    if (path.includes('/devices/') && method === 'PATCH') { await json(route, { id: device.id, friendly_name: device.friendly_name, measurement_scope: 'energy_only' }); return; }
    if (path.endsWith('/alerts')) { await json(route, alerts); return; }
    if (path.endsWith('/acknowledge')) { await json(route, { id: 'alert-backlog', state: 'acknowledged' }); return; }
    if (path.endsWith('/silence')) { await json(route, { id: 'alert-backlog', silenced_until: '2026-08-14T17:32:00Z' }); return; }
    if (path.endsWith('/history/export.csv')) { await route.fulfill({ status: 200, contentType: 'text/csv', body: 'timestamp,value\n2026-08-13T10:00:00Z,0\n' }); return; }
    if (path.endsWith('/history')) { await json(route, url.searchParams.get('resolution_seconds') === '86400' ? dailyHistory : history); return; }
    if (path.endsWith('/billing')) { await json(route, options.billingById?.[homeId] ?? billing); return; }
    if (path.endsWith('/bill-rate-imports')) { await json(route, { extractions: [] }); return; }
    if (path.endsWith('/rate-sources/status')) { await json(route, options.rateStatusById?.[homeId] ?? { ...rateSourceStatus, home_id: homeId }); return; }
    if (path.endsWith('/rate-sources/candidates') && method === 'GET') { await json(route, { home_id: homeId, candidates: candidatesFor(homeId) }); return; }
    if (path.endsWith('/rate-sources/runs')) { await json(route, { home_id: homeId, runs: [] }); return; }
    if (path.endsWith('/rate-sources/manual-candidates') && method === 'POST') {
      const body = request.postDataJSON() as { rate_plan_name?: string; rate_class?: string; effective_start?: string; effective_end?: string; source_title?: string; tariff_identifier?: string; source_url?: string; periods?: Array<Record<string, unknown>> };
      const candidateId = '00000000-0000-0000-0000-000000000070';
      const manual = structuredClone(rateCandidate) as MockRateCandidate;
      manual.id = candidateId;
      manual.source = { ...manual.source, id: '00000000-0000-0000-0000-000000000071', name: body.source_title ?? 'Manual SCE source', url: null, revision_id: '00000000-0000-0000-0000-000000000072', artifact_sha256: 'e'.repeat(64), parser_version: 'manual-rate-entry-v1' };
      const sourcePeriods = body.periods ?? manual.normalized_rates.plans[0]!.periods;
      manual.normalized_rates = { ...manual.normalized_rates, holiday_rule: 'administrator_entered_schedule', effective_start: body.effective_start ?? null, effective_end: body.effective_end ?? null, plans: [{ ...manual.normalized_rates.plans[0]!, rate_plan_name: body.rate_plan_name ?? 'MANUAL-TOU-D', rate_class: body.rate_class ?? 'residential', rate_components: 'administrator_entered_combined_price', periods: sourcePeriods.map((period) => {
        const periodName = typeof period.period_name === 'string' ? period.period_name : typeof period.name === 'string' ? period.name : 'all_day';
        return { season: period.season ?? 'all', day_type: period.day_type ?? 'all', name: periodName, start_minute: period.start_minute ?? 0, end_minute: period.end_minute ?? 1440, price_per_kwh: period.price_per_kwh ?? '0.00000001', currency: 'USD', unit: 'kWh', tier_min_kwh: null, tier_max_kwh: null };
      }) }] };
      manual.validation_evidence = { origin: 'manual_administrator_entry', parser_version: 'manual-rate-entry-v1', schema: 'sce-rate-candidate/1.0.0', coverage: 'complete', price_unit: 'USD/kWh', effective_date: 'administrator_review_required', source_title: body.source_title ?? 'Manual SCE source', tariff_identifier: body.tariff_identifier ?? 'Manual tariff', source_url: body.source_url ?? null, canonical_input_sha256: manual.source.artifact_sha256, canonical_input_bytes: 512, provenance_confirmation: 'administrator_attested_official_source' };
      manual.workflow = { state: 'review_required' };
      candidatesFor(homeId).unshift(manual);
      await json(route, { home_id: homeId, created: true, candidate_id: candidateId, revision_id: manual.source.revision_id, source_id: manual.source.id, run_id: 'rate-run-manual', state: 'review_required', canonical_input_sha256: manual.source.artifact_sha256, network_fetch_performed: false }, 201);
      return;
    }
    if (path.includes('/rate-sources/candidates/') && method === 'DELETE') {
      const candidateId = path.split('/').at(-1) ?? '';
      const candidates = candidatesFor(homeId);
      const index = candidates.findIndex((entry) => entry.id === candidateId);
      if (index < 0) { await problem(route, 404, 'rate candidate does not exist'); return; }
      candidates.splice(index, 1);
      await route.fulfill({ status: 204 });
      return;
    }
    if (path.includes('/rate-sources/candidates/') && path.endsWith('/reject') && method === 'POST') {
      rejectAttempts += 1;
      if (options.rateRejectDelayMs) await new Promise((resolve) => setTimeout(resolve, options.rateRejectDelayMs));
      if (options.rateRejectFailureOnce && rejectAttempts === 1) { await problem(route, 409, 'only an unpublished candidate can be rejected'); return; }
      const candidateId = path.split('/').at(-2) ?? '';
      const candidate = candidatesFor(homeId).find((entry) => entry.id === candidateId);
      if (!candidate) { await problem(route, 404, 'rate candidate does not exist'); return; }
      candidate.workflow = { id: candidate.workflow.id ?? 'review-rejected', state: 'rejected', selected_plan_name: candidate.workflow.selected_plan_name ?? null, effective_start: candidate.workflow.effective_start ?? null, effective_end: candidate.workflow.effective_end ?? null, reviewed_at: candidate.workflow.reviewed_at ?? '2026-08-13T17:00:00Z', published_at: null, activated_at: null, rate_plan_version_id: null, utility_account_id: null };
      await json(route, { home_id: homeId, candidate_id: candidateId, workflow: candidate.workflow });
      return;
    }
    if (path.includes('/rate-sources/candidates/') && path.endsWith('/review') && method === 'POST') {
      const candidateId = path.split('/').at(-2) ?? '';
      const candidate = candidatesFor(homeId).find((entry) => entry.id === candidateId);
      const body = request.postDataJSON() as { selected_plan_name: string; effective_start: string; effective_end?: string };
      if (!candidate) { await problem(route, 404, 'rate candidate does not exist'); return; }
      candidate.workflow = { id: 'review-1', state: 'reviewed', selected_plan_name: body.selected_plan_name, effective_start: body.effective_start, effective_end: body.effective_end ?? null, reviewed_at: '2026-08-13T17:00:00Z', published_at: null, activated_at: null, rate_plan_version_id: null, utility_account_id: null };
      await json(route, { home_id: homeId, candidate_id: candidateId, workflow: candidate.workflow });
      return;
    }
    if (path.includes('/rate-sources/candidates/') && path.endsWith('/publish') && method === 'POST') {
      const candidateId = path.split('/').at(-2) ?? '';
      const candidate = candidatesFor(homeId).find((entry) => entry.id === candidateId);
      if (!candidate) { await problem(route, 404, 'rate candidate does not exist'); return; }
      candidate.workflow = { ...candidate.workflow, state: 'published', published_at: '2026-08-13T17:01:00Z', rate_plan_version_id: 'rate-version-new' };
      await json(route, { home_id: homeId, candidate_id: candidateId, workflow: candidate.workflow, rate_plan_version: { id: 'rate-version-new', plan_id: 'rate-plan-1', plan_name: candidate.workflow.selected_plan_name, version: 2, effective_start: candidate.workflow.effective_start, effective_end: candidate.workflow.effective_end ?? null, source_artifact_sha256: candidate.source.artifact_sha256, state: 'published' } }, 201);
      return;
    }
    if (path.includes('/rate-sources/candidates/') && path.endsWith('/activate') && method === 'POST') {
      const candidateId = path.split('/').at(-2) ?? '';
      const candidate = candidatesFor(homeId).find((entry) => entry.id === candidateId);
      const body = request.postDataJSON() as { utility_account_id: string };
      if (!candidate) { await problem(route, 404, 'rate candidate does not exist'); return; }
      candidate.workflow = { ...candidate.workflow, state: 'activated', activated_at: '2026-08-13T17:02:00Z', utility_account_id: body.utility_account_id };
      await json(route, { home_id: homeId, candidate_id: candidateId, workflow: candidate.workflow, assignment: { id: 'assignment-new', utility_account_id: body.utility_account_id, rate_plan_version_id: candidate.workflow.rate_plan_version_id, effective_start: candidate.workflow.effective_start, effective_end: candidate.workflow.effective_end ?? null } }, 201);
      return;
    }
    if (path.endsWith('/rate-sources/check-now')) { await json(route, options.rateCheckOverride ?? { run_id: 'rate-run-1', state: 'review_required', event_code: 'RATE_SOURCE_CHANGED', revision_id: rateCandidate.source.revision_id, candidate_id: rateCandidate.id, error_code: null }, 202); return; }
    if (path.endsWith('/users') && method === 'POST') { await json(route, { id: 'user-new', email: 'new@example.test', display_name: 'New User' }, 201); return; }
    if (path.endsWith('/users')) { await json(route, { users: [session.user] }); return; }
    if (path.endsWith('/roles')) { await json(route, { roles: [{ id: 'role-owner', name: 'Owner', permissions: session.user.permissions, built_in: true }], available_permissions: session.user.permissions }); return; }
    if (path.endsWith('/system/health')) { await json(route, systemHealth); return; }
    if (path.endsWith('/backups/status')) { await json(route, backupStatus); return; }
    if (path.endsWith('/firmware/releases') && method === 'POST') { await json(route, { release: firmwareReleases.releases[0], manifest_signature: 'fixture-signature', physical_certification: 'pending' }, 201); return; }
    if (path.includes('/firmware/releases/') && method === 'DELETE') { await route.fulfill({ status: 204 }); return; }
    if (path.endsWith('/firmware/releases')) { await json(route, firmwareReleases); return; }
    if (path.includes('/firmware/releases/') && path.endsWith('/deploy')) { await json(route, { deployments: [{ id: 'deployment-1', device_id: device.id, state: 'queued' }] }, 202); return; }
    if (path.endsWith('/commands')) {
      if (options.forbiddenCommands) { await problem(route, 403, 'The server refused this command.'); return; }
      const body = request.postDataJSON() as { command_type: string };
      commandTypes.push(body.command_type);
      if (body.command_type === 'format_storage_prepare') {
        formatPrepared = true;
        await json(route, { command: { id: '00000000-0000-0000-0000-000000000001', type: body.command_type, state: 'queued' }, confirmation_token: 'bound-token' }, 202);
      } else {
        await json(route, { command: { id: `00000000-0000-0000-0000-00000000000${commandTypes.length}`, type: body.command_type, state: 'queued' }, confirmation_token: null }, 202);
      }
      return;
    }
    if (path.includes('/users/') && method === 'PATCH') { await json(route, { id: session.user.id, enabled: true, display_name: session.user.display_name }); return; }
    if (path.endsWith('/diagnostics/bundle')) { await route.fulfill({ status: 200, contentType: 'application/zip', body: 'redacted' }); return; }

    const fallback = apiResponse(path, method);
    await route.fulfill({ status: fallback.status, contentType: fallback.contentType ?? 'application/json', body: fallback.status === 204 ? '' : JSON.stringify(fallback.body) });
  });

  await page.route('**/api/v1/events', async (route) => {
    await route.fulfill({ status: 200, contentType: 'text/event-stream', headers: { 'Cache-Control': 'no-cache' }, body: ': keepalive\n\n' });
  });
  return commandTypes;
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function problem(route: Route, status: number, detail: string) {
  await route.fulfill({ status, contentType: 'application/problem+json', body: JSON.stringify({ type: 'about:blank', title: status === 403 ? 'Forbidden' : 'Request failed', status, detail }) });
}
