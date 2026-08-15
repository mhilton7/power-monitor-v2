import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { alerts, apiResponse, backupStatus, billing, circuits, dailyHistory, device, firmwareReleases, history, home, homeScopes, homeUtility, rateCandidate, session, systemHealth } from '../fixtures.ts';

const server = createServer((request, response) => {
  const url = new URL(request.url ?? '/', 'http://127.0.0.1:8000');
  const path = url.pathname;
  response.setHeader('Access-Control-Allow-Origin', 'http://127.0.0.1:4173');
  response.setHeader('Cache-Control', 'no-store');

  if (path.endsWith('/events')) {
    response.writeHead(200, { 'Content-Type': 'text/event-stream', Connection: 'keep-alive' });
    response.write(': keepalive\n\n');
    return;
  }
  if (path.endsWith('/auth/bootstrap/status')) return json(response, 200, { required: false });
  if (path.endsWith('/auth/me')) return json(response, 200, session.user);
  if (path.endsWith('/auth/login') || path.endsWith('/auth/bootstrap')) return json(response, 200, { user: session.user });
  if (path.endsWith('/auth/logout')) { response.writeHead(204); response.end(); return; }
  if (path.endsWith('/home-scopes')) return json(response, 200, { home_scopes: homeScopes });
  if (path.endsWith('/settings/home-utility')) return json(response, 200, homeUtility);
  if (path.endsWith('/home')) return json(response, 200, home);
  if (path.endsWith('/enrollment-tokens') && request.method === 'POST') return json(response, 201, { token: 'single-use-enrollment-token-value-000000000000', expires_at: '2026-08-13T17:47:00Z' });
  if (path.endsWith('/credentials/rotate') && path.includes('/devices/') && request.method === 'POST') return json(response, 202, { rotation: { rotation_id: '00000000-0000-0000-0000-000000000050', credential_fingerprint: 'b'.repeat(64), state: 'pending', overlap_expires_at: '2026-08-13T17:42:10Z', prepare_command_id: '00000000-0000-0000-0000-000000000051', commit_command_id: null, cancel_command_id: null } });
  if (path.endsWith('/cancel') && path.includes('/credentials/rotations/') && request.method === 'POST') return json(response, 202, { rotation: { rotation_id: '00000000-0000-0000-0000-000000000050', credential_fingerprint: 'b'.repeat(64), state: 'pending', overlap_expires_at: '2026-08-13T17:42:10Z', prepare_command_id: '00000000-0000-0000-0000-000000000051', commit_command_id: null, cancel_command_id: '00000000-0000-0000-0000-000000000052' } });
  if (path.endsWith('/circuits/verified-aggregates') && request.method === 'POST') return json(response, 201, { id: '00000000-0000-0000-0000-000000000040', name: 'Verified whole home', device_ids: ['device-main', 'device-secondary'] });
  if (path.endsWith('/circuits')) return json(response, 200, circuits);
  if (path.endsWith('/devices')) return json(response, 200, { home_scopes: homeScopes, devices: [device] });
  if (path.endsWith('/revoke') && path.includes('/devices/') && request.method === 'POST') { response.writeHead(204); response.end(); return; }
  if (path.includes('/devices/') && request.method === 'PATCH') return json(response, 200, { id: device.id, friendly_name: device.friendly_name, measurement_scope: 'energy_only' });
  if (path.endsWith('/alerts')) return json(response, 200, alerts);
  if (path.endsWith('/acknowledge')) return json(response, 200, { id: 'alert-backlog', state: 'acknowledged' });
  if (path.endsWith('/silence')) return json(response, 200, { id: 'alert-backlog', silenced_until: '2026-08-14T17:32:00Z' });
  if (path.endsWith('/history/export.csv')) { response.writeHead(200, { 'Content-Type': 'text/csv' }); response.end('timestamp,value\n2026-08-13T10:00:00Z,0\n'); return; }
  if (path.endsWith('/history')) return json(response, 200, url.searchParams.get('resolution_seconds') === '86400' ? dailyHistory : history);
  if (path.endsWith('/billing')) return json(response, 200, billing);
  if (path.endsWith('/bill-rate-imports')) return json(response, 200, { extractions: [] });
  if (path.endsWith('/rate-sources/check-now')) return json(response, 202, { run_id: 'rate-run-1', state: 'review_required', event_code: 'RATE_SOURCE_CHANGED', revision_id: rateCandidate.source.revision_id, candidate_id: rateCandidate.id, error_code: null });
  if (path.endsWith('/users') && request.method === 'POST') return json(response, 201, { id: 'user-new', email: 'new@example.test', display_name: 'New User' });
  if (path.endsWith('/users')) return json(response, 200, { users: [session.user] });
  if (path.endsWith('/roles')) return json(response, 200, { roles: [{ id: 'role-owner', name: 'Owner', permissions: session.user.permissions, built_in: true }], available_permissions: session.user.permissions });
  if (path.endsWith('/system/health')) return json(response, 200, systemHealth);
  if (path.endsWith('/backups/status')) return json(response, 200, backupStatus);
  if (path.endsWith('/firmware/releases') && request.method === 'POST') return json(response, 201, { release: firmwareReleases.releases[0], manifest_signature: 'fixture-signature', physical_certification: 'pending' });
  if (path.endsWith('/firmware/releases')) return json(response, 200, firmwareReleases);
  if (path.includes('/firmware/releases/') && path.endsWith('/deploy')) return json(response, 202, { deployments: [{ id: 'deployment-1', device_id: device.id, state: 'queued' }] });
  if (path.endsWith('/commands')) return collectJson(request, (body) => {
    const command = body as { command_type?: string };
    if (command.command_type === 'format_storage_prepare') return json(response, 202, { command: { id: 'prepare-command-00000000-0000-0000-0000-000000000001', type: command.command_type, state: 'queued' }, confirmation_token: 'bound-token' });
    return json(response, 202, { command: { id: 'command-00000000-0000-0000-0000-000000000001', type: command.command_type ?? 'unknown', state: 'queued' }, confirmation_token: null });
  });
  const fallback = apiResponse(path, request.method ?? 'GET');
  return json(response, fallback.status, fallback.body ?? {});
});

server.listen(8000, '127.0.0.1', () => console.log('PowerMeter V2 E2E mock API listening on http://127.0.0.1:8000'));

function json(response: ServerResponse, status: number, body: unknown) {
  response.writeHead(status, { 'Content-Type': 'application/json' });
  response.end(JSON.stringify(body));
}

function collectJson(request: IncomingMessage, callback: (value: unknown) => void) {
  let input = '';
  request.setEncoding('utf8');
  request.on('data', (chunk: string) => { input += chunk; });
  request.on('end', () => callback(input ? JSON.parse(input) as unknown : {}));
}
