import type { Page, Route } from '@playwright/test';
import { alerts, apiResponse, backupStatus, billing, circuits, dailyHistory, device, firmwareReleases, history, home, homeUtility, session, systemHealth } from '../fixtures';

interface MockOptions {
  sessionExpired?: boolean;
  forbiddenCommands?: boolean;
  homeOverride?: Record<string, unknown>;
}

export async function mockApi(page: Page, options: MockOptions = {}) {
  await page.clock.setFixedTime(new Date('2026-08-13T17:32:15Z'));
  const commandTypes: string[] = [];
  let formatPrepared = false;

  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (options.sessionExpired && path.endsWith('/auth/me')) {
      await route.fulfill({ status: 401, contentType: 'application/problem+json', body: JSON.stringify({ type: 'about:blank', title: 'Unauthorized', status: 401, detail: 'Session expired.' }) });
      return;
    }
    if (path.endsWith('/auth/bootstrap/status')) { await json(route, { required: false }); return; }
    if (path.endsWith('/auth/me')) { await json(route, session.user); return; }
    if (path.endsWith('/auth/login') || path.endsWith('/auth/bootstrap')) { await json(route, { user: session.user }); return; }
    if (path.endsWith('/auth/logout')) { await route.fulfill({ status: 204 }); return; }
    if (path.endsWith('/settings/home-utility')) { await json(route, homeUtility); return; }
    if (path.endsWith('/home')) { await json(route, options.homeOverride ?? home); return; }
    if (path.endsWith('/enrollment-tokens') && method === 'POST') { await json(route, { token: 'single-use-enrollment-token-value-000000000000', expires_at: '2026-08-13T17:47:00Z' }, 201); return; }
    if (path.endsWith('/credentials/rotate') && path.includes('/devices/') && method === 'POST') { await json(route, { rotation: { rotation_id: '00000000-0000-0000-0000-000000000050', credential_fingerprint: 'b'.repeat(64), state: 'pending', overlap_expires_at: '2026-08-13T17:42:10Z', prepare_command_id: '00000000-0000-0000-0000-000000000051', commit_command_id: null, cancel_command_id: null } }, 202); return; }
    if (path.endsWith('/cancel') && path.includes('/credentials/rotations/') && method === 'POST') { await json(route, { rotation: { rotation_id: '00000000-0000-0000-0000-000000000050', credential_fingerprint: 'b'.repeat(64), state: 'pending', overlap_expires_at: '2026-08-13T17:42:10Z', prepare_command_id: '00000000-0000-0000-0000-000000000051', commit_command_id: null, cancel_command_id: '00000000-0000-0000-0000-000000000052' } }, 202); return; }
    if (path.endsWith('/circuits/verified-aggregates') && method === 'POST') { await json(route, { id: '00000000-0000-0000-0000-000000000040', name: 'Verified whole home', device_ids: ['device-main', 'device-secondary'] }, 201); return; }
    if (path.endsWith('/circuits')) { await json(route, circuits); return; }
    if (path.endsWith('/devices')) { await json(route, { devices: [{ ...device, ...(formatPrepared ? { last_command: { id: '00000000-0000-0000-0000-000000000001', type: 'format_storage_prepare', state: 'succeeded', progress_percent: 100, result_code: 'ok', result_evidence: { prepare_command_id: '00000000-0000-0000-0000-000000000001', acknowledged_records_lost: 42, unacknowledged_records_lost: 5, ready: true } } } : {}) }] }); return; }
    if (path.endsWith('/revoke') && path.includes('/devices/') && method === 'POST') { await route.fulfill({ status: 204 }); return; }
    if (path.includes('/devices/') && method === 'PATCH') { await json(route, { id: device.id, friendly_name: device.friendly_name, measurement_scope: 'energy_only' }); return; }
    if (path.endsWith('/alerts')) { await json(route, alerts); return; }
    if (path.endsWith('/acknowledge')) { await json(route, { id: 'alert-backlog', state: 'acknowledged' }); return; }
    if (path.endsWith('/silence')) { await json(route, { id: 'alert-backlog', silenced_until: '2026-08-14T17:32:00Z' }); return; }
    if (path.endsWith('/history/export.csv')) { await route.fulfill({ status: 200, contentType: 'text/csv', body: 'timestamp,value\n2026-08-13T10:00:00Z,0\n' }); return; }
    if (path.endsWith('/history')) { await json(route, url.searchParams.get('resolution_seconds') === '86400' ? dailyHistory : history); return; }
    if (path.endsWith('/billing')) { await json(route, billing); return; }
    if (path.endsWith('/bill-rate-imports')) { await json(route, { extractions: [] }); return; }
    if (path.endsWith('/rate-sources/check-now')) { await json(route, { run_id: 'rate-run-1', state: 'review_required' }, 202); return; }
    if (path.endsWith('/users') && method === 'POST') { await json(route, { id: 'user-new', email: 'new@example.test', display_name: 'New User' }, 201); return; }
    if (path.endsWith('/users')) { await json(route, { users: [session.user] }); return; }
    if (path.endsWith('/roles')) { await json(route, { roles: [{ id: 'role-owner', name: 'Owner', permissions: session.user.permissions, built_in: true }], available_permissions: session.user.permissions }); return; }
    if (path.endsWith('/system/health')) { await json(route, systemHealth); return; }
    if (path.endsWith('/backups/status')) { await json(route, backupStatus); return; }
    if (path.endsWith('/firmware/releases') && method === 'POST') { await json(route, { release: firmwareReleases.releases[0], manifest_signature: 'fixture-signature', physical_certification: 'pending' }, 201); return; }
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
