import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SettingsPage } from '../src/pages/SettingsPage';
import { useHomeScope } from '../src/home/useHomeScope';
import { apiResponse, device, firmwareReleases, homeUtility, systemHealth } from './fixtures';
import { installFetchMock, renderWithProviders } from './render';

describe('Settings', () => {
  it('enrolls the first sensor with the only authorized home scope', async () => {
    const homeId = '00000000-0000-0000-0000-000000000010';
    let enrollment: Record<string, unknown> | undefined;
    installFetchMock((path, method, body) => {
      if (path.includes('/devices?')) {
        return { status: 200, body: { home_scopes: [{ id: homeId, name: 'Home' }], devices: [] } };
      }
      if (path.endsWith('/enrollment-tokens') && method === 'POST') {
        if (typeof body !== 'string') throw new Error('Expected JSON enrollment body.');
        enrollment = JSON.parse(body) as Record<string, unknown>;
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<SettingsPage />);
    await userEvent.click(await screen.findByRole('button', { name: 'Sensors' }));
    expect(await screen.findByText(/No sensors are enrolled yet/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Enroll sensor' }));
    expect(screen.getByText('Enrollment home: Home')).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText('Friendly name'), 'Main panel');
    await userEvent.click(screen.getByRole('button', { name: 'Create token' }));
    expect(await screen.findByText('single-use-enrollment-token-value-000000000000')).toBeInTheDocument();
    expect(enrollment).toMatchObject({ home_id: homeId, friendly_name: 'Main panel' });
  });

  it('requires an explicit home selection when multiple sensor scopes are authorized', async () => {
    const firstHomeId = '00000000-0000-0000-0000-000000000010';
    const secondHomeId = '00000000-0000-0000-0000-000000000011';
    const scopes = [{ id: firstHomeId, name: 'Home' }, { id: secondHomeId, name: 'Home' }];
    let enrollment: Record<string, unknown> | undefined;
    installFetchMock((path, method, body) => {
      if (path.includes('/devices?')) return { status: 200, body: { home_scopes: scopes, devices: [] } };
      if (path.endsWith('/enrollment-tokens') && method === 'POST') {
        if (typeof body !== 'string') throw new Error('Expected JSON enrollment body.');
        enrollment = JSON.parse(body) as Record<string, unknown>;
      }
      return apiResponse(path, method);
    });
    function Harness() {
      const { homeScopes, selectedHomeId, setSelectedHomeId } = useHomeScope();
      return <><label htmlFor="test-active-home">Active home</label><select id="test-active-home" value={selectedHomeId} onChange={(event) => setSelectedHomeId(event.target.value)}><option value="">Select an active home</option>{homeScopes.map((home) => <option key={home.id} value={home.id}>{home.name} ({home.id})</option>)}</select><SettingsPage /></>;
    }
    renderWithProviders(<Harness />, { homeScopes: scopes });
    expect(screen.getByRole('option', { name: `Home (${firstHomeId})` })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: `Home (${secondHomeId})` })).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('Active home'), secondHomeId);
    await userEvent.click(await screen.findByRole('button', { name: 'Sensors' }));
    await userEvent.click(screen.getByRole('button', { name: 'Enroll sensor' }));
    const create = screen.getByRole('button', { name: 'Create token' });
    await userEvent.type(screen.getByLabelText('Friendly name'), 'Workshop panel');
    expect(create).toBeEnabled();
    await userEvent.click(create);
    await waitFor(() => expect(enrollment).toMatchObject({ home_id: secondHomeId }));
  });

  it('keeps enrollment fail-closed when no authorized home scope is returned', async () => {
    installFetchMock((path, method) => path.includes('/devices?')
      ? { status: 200, body: { home_scopes: [], devices: [] } }
      : apiResponse(path, method));
    renderWithProviders(<SettingsPage />, { homeScopes: [] });
    await userEvent.click(await screen.findByRole('button', { name: 'Sensors' }));
    await userEvent.click(screen.getByRole('button', { name: 'Enroll sensor' }));
    expect(screen.getByText(/No authorized sensor home scope is available/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create token' })).toBeDisabled();
  });

  it('requires the exact typed confirmation before enabling full-account scope', async () => {
    let update: Record<string, unknown> | undefined;
    installFetchMock((path, method, body) => {
      if (path.includes('/settings/home-utility') && method === 'PATCH') {
        if (typeof body !== 'string') throw new Error('Expected JSON settings body.');
        update = JSON.parse(body) as Record<string, unknown>;
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<SettingsPage />);
    await userEvent.selectOptions(await screen.findByLabelText('Cost scope'), 'full_account');
    await userEvent.type(screen.getByLabelText(/Type I UNDERSTAND FULL ACCOUNT SCOPE/), 'I UNDERSTAND FULL ACCOUNT SCOPE');
    await userEvent.click(screen.getByRole('button', { name: 'Save home settings' }));
    await waitFor(() => expect(update).toMatchObject({ cost_scope: 'full_account', full_account_confirmation: 'I UNDERSTAND FULL ACCOUNT SCOPE' }));
  });

  it('refreshes the visible home name immediately after rename without showing its UUID', async () => {
    const homeId = '00000000-0000-0000-0000-000000000010';
    let renamed = false;
    installFetchMock((path, method) => {
      if (path.includes('/settings/home-utility') && method === 'PATCH') {
        renamed = true;
        return { status: 200, body: { ...homeUtility, home: { id: homeId, name: 'Primary residence', timezone: 'America/Los_Angeles' } } };
      }
      if (path.endsWith('/home-scopes') && renamed) return { status: 200, body: { home_scopes: [{ id: homeId, name: 'Primary residence' }] } };
      return apiResponse(path, method);
    });
    function Harness() {
      const { selectedHome } = useHomeScope();
      return <><div data-testid="visible-home-name">{selectedHome?.name}</div><SettingsPage /></>;
    }
    renderWithProviders(<Harness />);
    await userEvent.clear(await screen.findByLabelText('Home name'));
    await userEvent.type(screen.getByLabelText('Home name'), 'Primary residence');
    await userEvent.click(screen.getByRole('button', { name: 'Save home settings' }));
    await waitFor(() => expect(screen.getByTestId('visible-home-name')).toHaveTextContent('Primary residence'));
    expect(screen.queryByText(homeId)).not.toBeInTheDocument();
  });

  it('persists sensor presentation and monitoring settings without exposing a home UUID', async () => {
    let update: Record<string, unknown> | undefined;
    installFetchMock((path, method, body) => {
      if (path.includes('/devices/') && method === 'PATCH') {
        if (typeof body !== 'string') throw new Error('Expected JSON sensor settings body.');
        update = JSON.parse(body) as Record<string, unknown>;
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<SettingsPage />);
    await userEvent.click(await screen.findByRole('button', { name: 'Sensors' }));
    expect(screen.queryByText('00000000-0000-0000-0000-000000000010')).not.toBeInTheDocument();
    await userEvent.click(await screen.findByRole('button', { name: /Main Panel Sensor/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Configure sensor' }));
    await userEvent.clear(screen.getByLabelText('Location'));
    await userEvent.type(screen.getByLabelText('Location'), 'Garage panel');
    await userEvent.type(screen.getByLabelText('Notes'), 'Verified one-CT sensor');
    await userEvent.clear(screen.getByLabelText('Display order'));
    await userEvent.type(screen.getByLabelText('Display order'), '4');
    await userEvent.click(screen.getByRole('checkbox', { name: /Eligible for service branches/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save sensor' }));
    await waitFor(() => expect(update).toMatchObject({
      location: 'Garage panel', notes: 'Verified one-CT sensor', display_order: 4,
      include_in_aggregate: false, show_on_dashboard: true, monitoring_enabled: true,
    }));
  });

  it('updates a service branch and protects the current Main service from deletion', async () => {
    let branchUpdate: Record<string, unknown> | undefined;
    let deletedBranch = '';
    installFetchMock((path, method, body) => {
      if (path.includes('/circuits/circuit-main-service') && method === 'PATCH') {
        if (typeof body !== 'string') throw new Error('Expected JSON service-branch body.');
        branchUpdate = JSON.parse(body) as Record<string, unknown>;
        return apiResponse(path, method);
      }
      if (path.includes('/circuits/circuit-main-service') && method === 'DELETE') {
        deletedBranch = path;
        return { status: 204 };
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<SettingsPage />);
    await userEvent.click(await screen.findByRole('button', { name: 'Sensors' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Manage' }));
    await userEvent.clear(screen.getByLabelText('Service branch name'));
    await userEvent.type(screen.getByLabelText('Service branch name'), 'Main service updated');
    await userEvent.type(screen.getByLabelText(/Type I VERIFIED THESE NON-OVERLAPPING METERS/), 'I VERIFIED THESE NON-OVERLAPPING METERS');
    await userEvent.click(screen.getByRole('button', { name: 'Save service branch' }));
    await waitFor(() => expect(branchUpdate).toMatchObject({
      name: 'Main service updated', purpose: 'whole_home_total', is_home_total: true,
      device_ids: ['device-main'], confirmation: 'I VERIFIED THESE NON-OVERLAPPING METERS',
    }));

    await userEvent.click(await screen.findByRole('button', { name: 'Manage' }));
    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled();
    expect(screen.getByText(/Choose and save a replacement Main service billing source before deleting/)).toBeInTheDocument();
    expect(deletedBranch).toBe('');
  });

  it('requires the exact confirmation before shortening server History retention', async () => {
    let telemetryUpdate: Record<string, unknown> | undefined;
    installFetchMock((path, method, body) => {
      if (path.includes('/settings/telemetry') && method === 'PATCH') {
        if (typeof body !== 'string') throw new Error('Expected JSON telemetry settings body.');
        telemetryUpdate = JSON.parse(body) as Record<string, unknown>;
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<SettingsPage />);
    await userEvent.click(await screen.findByRole('button', { name: 'Sensors' }));
    await userEvent.selectOptions(await screen.findByLabelText('History retention'), '90');
    await userEvent.click(screen.getByRole('button', { name: 'Save reading settings' }));
    const dialog = screen.getByRole('dialog', { name: 'Shorten saved History retention?' });
    expect(within(dialog).getByRole('button', { name: 'Shorten retention' })).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText('Type DELETE EXPIRED SAVED HISTORY'), 'DELETE EXPIRED SAVED HISTORY');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Shorten retention' }));
    await waitFor(() => expect(telemetryUpdate).toMatchObject({ retention_days: 90, retention_confirmation: 'DELETE EXPIRED SAVED HISTORY' }));
    expect(telemetryUpdate).not.toHaveProperty('config_version');
  });

  it('saves server-persisted per-user display preferences', async () => {
    let preferences: Record<string, unknown> | undefined;
    installFetchMock((path, method, body) => {
      if (path.endsWith('/auth/preferences') && method === 'PUT') {
        if (typeof body !== 'string') throw new Error('Expected JSON preferences body.');
        preferences = JSON.parse(body) as Record<string, unknown>;
        return { status: 200, body: { preferences } };
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<SettingsPage />);
    await userEvent.click(await screen.findByRole('button', { name: 'Appearance' }));
    await userEvent.selectOptions(await screen.findByLabelText('Refresh interval'), '120');
    await userEvent.selectOptions(screen.getByLabelText('Power unit'), 'W');
    await userEvent.click(screen.getByRole('radio', { name: /Compact/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save display preferences' }));
    await waitFor(() => expect(preferences).toMatchObject({ refresh_seconds: 120, power_unit: 'W', density: 'compact' }));
    expect(await screen.findByText('Display preferences were saved to your account.')).toBeInTheDocument();
  });

  it('shows backup verification and isolated restore evidence', async () => {
    installFetchMock();
    renderWithProviders(<SettingsPage />);
    await userEvent.click(await screen.findByRole('button', { name: /Backups & restore/ }));
    expect(screen.getByText('Backup checksum')).toBeInTheDocument();
    expect(screen.getByText('Last isolated restore test')).toBeInTheDocument();
    expect(screen.getByText(/file existence alone is never reported as verification/)).toBeInTheDocument();
  });

  it('renders server-forbidden permission changes as an error', async () => {
    installFetchMock((path, method) => path.includes('/users/') && method === 'PATCH'
      ? { status: 403, body: { type: 'about:blank', title: 'Forbidden', status: 403, detail: 'Owner permission is protected.' } }
      : apiResponse(path, method));
    renderWithProviders(<SettingsPage />);
    await userEvent.click(await screen.findByRole('button', { name: /Profile & users/ }));
    await userEvent.click(screen.getByRole('button', { name: /Alex Morgan/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    await userEvent.click(screen.getByRole('button', { name: 'Apply changes' }));
    expect(await screen.findByText('The server refused this account change.')).toBeInTheDocument();
  });

  it('shows exact system health evidence', async () => {
    installFetchMock((path, method) => {
      const response = apiResponse(path, method);
      if (path.endsWith('/system/health')) {
        return {
          status: 200,
          body: {
            ...systemHealth,
            sensors: [
              { ...systemHealth.sensors[0]!, acknowledgement: null },
              { ...systemHealth.sensors[0]!, device_id: 'device-outdoor', device_name: 'Outdoor AC', acknowledgement: null },
            ],
          },
        };
      }
      if (path.includes('/devices?')) {
        return {
          status: 200,
          body: {
            devices: [
              { ...device, acknowledgement: null },
              { ...device, id: 'device-outdoor', friendly_name: 'Outdoor AC', acknowledgement: null },
            ],
          },
        };
      }
      return response;
    });
    renderWithProviders(<SettingsPage />);
    await userEvent.click(await screen.findByRole('button', { name: 'Diagnostics' }));
    expect(screen.getByText('reachable')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Sensor delivery' })).toBeInTheDocument();
    expect(screen.getAllByText('received')).toHaveLength(2);
    const diagnostics = screen.getByRole('heading', { name: 'Diagnostics' }).closest('.card');
    expect(diagnostics).not.toBeNull();
    expect(within(diagnostics as HTMLElement).getByText('Frontend')).toBeInTheDocument();
    expect(within(diagnostics as HTMLElement).getByText('Backend')).toBeInTheDocument();
    expect(within(diagnostics as HTMLElement).getAllByText('not supplied').length).toBeGreaterThanOrEqual(1);
    expect(within(diagnostics as HTMLElement).getAllByText(/Not reported/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('v1.2.3 · build not reported')).toHaveLength(2);
  });

  it('permanently deletes an eligible firmware release only after exact confirmation', async () => {
    let deletion: { path: string; body: Record<string, unknown> } | undefined;
    installFetchMock((path, method, body) => {
      if (path.endsWith('/delete-permanently') && method === 'POST') {
        if (typeof body !== 'string') throw new Error('Expected firmware deletion confirmation JSON.');
        deletion = { path, body: JSON.parse(body) as Record<string, unknown> };
        return { status: 204 };
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<SettingsPage />);

    await userEvent.click(await screen.findByRole('button', { name: 'Firmware' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Delete permanently' }));
    expect(screen.getByRole('dialog', { name: /Delete firmware .* permanently\?/ })).toBeInTheDocument();
    const confirm = screen.getByRole('button', { name: 'Delete release permanently' });
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText('Type DELETE RELEASE PERMANENTLY'), 'DELETE RELEASE PERMANENTLY');
    await userEvent.click(confirm);
    await waitFor(() => expect(deletion).toMatchObject({
      path: expect.stringContaining('/firmware/releases/00000000-0000-0000-0000-000000000030/delete-permanently'),
      body: { confirmation: 'DELETE RELEASE PERMANENTLY', semantic_version: '1.2.4', build_number: '851', sha256: 'b'.repeat(64) },
    }));
  });

  it('hides archived releases by default and restores them without offering Deploy', async () => {
    const archived = {
      ...firmwareReleases.releases[0]!,
      release_id: '00000000-0000-0000-0000-000000000034',
      semantic_version: '1.2.2',
      release_state: 'archived',
      deploy_eligible: false,
      archive_eligible: false,
      restore_eligible: true,
      delete_eligibility: { eligible: true, protection_reasons: [] },
      deployment_batches: [],
    };
    let restored = '';
    installFetchMock((path, method) => {
      const pathname = new URL(path, 'http://frontend.test').pathname;
      if (pathname.endsWith('/firmware/releases') && method === 'GET') return { status: 200, body: { releases: [...firmwareReleases.releases, archived] } };
      if (pathname.endsWith('/restore') && method === 'POST') { restored = path; return { status: 200, body: { ...archived, release_state: 'available', restore_eligible: false, archive_eligible: true, deploy_eligible: true } }; }
      return apiResponse(path, method);
    });
    renderWithProviders(<SettingsPage />);

    await userEvent.click(await screen.findByRole('button', { name: 'Firmware' }));
    expect(await screen.findByText('1.2.4 · build 851')).toBeInTheDocument();
    expect(screen.queryByText('1.2.2 · build 851')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Show archived' }));
    const archivedHeading = await screen.findByText('1.2.2 · build 851');
    const archivedCard = archivedHeading.closest('.firmware-release-item');
    expect(archivedCard).not.toBeNull();
    expect(within(archivedCard as HTMLElement).queryByRole('button', { name: 'Deploy' })).not.toBeInTheDocument();
    expect(within(archivedCard as HTMLElement).queryByRole('button', { name: 'Protect for rollback' })).not.toBeInTheDocument();
    await userEvent.click(within(archivedCard as HTMLElement).getByRole('button', { name: 'Restore release' }));
    await waitFor(() => expect(restored).toContain('/firmware/releases/00000000-0000-0000-0000-000000000034/restore'));
  });

  it('archives terminal deployments and confirms bounded history retention', async () => {
    const requests: Array<{ path: string; body: Record<string, unknown> }> = [];
    installFetchMock((path, method, body) => {
      if ((path.endsWith('/archive') || path.endsWith('/firmware/lifecycle-settings')) && (method === 'POST' || method === 'PATCH')) {
        if (typeof body !== 'string') throw new Error('Expected lifecycle request JSON.');
        requests.push({ path, body: JSON.parse(body) as Record<string, unknown> });
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<SettingsPage />);

    await userEvent.click(await screen.findByRole('button', { name: 'Firmware' }));
    await userEvent.click(await screen.findByText('Deployment summary (1)'));
    await userEvent.click(screen.getByRole('button', { name: 'Archive deployment' }));
    await userEvent.click(within(screen.getByRole('dialog', { name: 'Archive this completed deployment?' }))
      .getByRole('button', { name: 'Archive deployment' }));
    await waitFor(() => expect(requests[0]).toMatchObject({ body: { confirmation: 'ARCHIVE DEPLOYMENT RECORD' } }));

    await userEvent.selectOptions(await screen.findByLabelText('Keep archived deployment details'), '90');
    await userEvent.click(screen.getByRole('button', { name: 'Review retention change' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save retention policy' }));
    await waitFor(() => expect(requests[1]).toMatchObject({ body: { deployment_retention_days: 90, confirmation: 'DELETE EXPIRED DEPLOYMENT HISTORY' } }));
  });

  it('makes an eligible release current and changes rollback protection only after confirmation', async () => {
    const requests: Array<{ path: string; body: Record<string, unknown> }> = [];
    installFetchMock((path, method, body) => {
      if ((path.endsWith('/make-current') && method === 'POST') || (path.endsWith('/rollback-pin') && method === 'PATCH')) {
        if (typeof body !== 'string') throw new Error('Expected firmware lifecycle JSON.');
        requests.push({ path, body: JSON.parse(body) as Record<string, unknown> });
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<SettingsPage />);

    await userEvent.click(await screen.findByRole('button', { name: 'Firmware' }));
    await userEvent.click(screen.getByRole('button', { name: 'Make current' }));
    const currentDialog = screen.getByRole('dialog', { name: 'Make firmware 1.2.4 current?' });
    await userEvent.click(within(currentDialog).getByRole('button', { name: 'Make current firmware' }));
    await waitFor(() => expect(requests[0]).toMatchObject({
      path: expect.stringContaining('/make-current'),
      body: { confirmation: 'MAKE CURRENT FIRMWARE', semantic_version: '1.2.4', sha256: 'b'.repeat(64) },
    }));

    await userEvent.click(screen.getByRole('button', { name: 'Protect for rollback' }));
    const rollbackDialog = screen.getByRole('dialog', { name: 'Protect firmware 1.2.4?' });
    await userEvent.click(within(rollbackDialog).getByRole('button', { name: 'Protect for rollback' }));
    await waitFor(() => expect(requests[1]).toMatchObject({
      path: expect.stringContaining('/rollback-pin'),
      body: { confirmation: 'UPDATE ROLLBACK PROTECTION', rollback_pinned: true },
    }));
  });

  it('shows independent partial OTA results and retries only the failed sensor', async () => {
    let retryRequest: { path: string; body: Record<string, unknown> } | undefined;
    installFetchMock((path, method, body) => {
      if (path.includes('/firmware/deployment-batches/') && path.endsWith('/retry') && method === 'POST') {
        if (typeof body !== 'string') throw new Error('Expected retry request JSON body');
        retryRequest = { path, body: JSON.parse(body) as Record<string, unknown> };
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<SettingsPage />);

    await userEvent.click(await screen.findByRole('button', { name: 'Firmware' }));
    expect(screen.getByText('Indoor-AC')).not.toBeVisible();
    await userEvent.click(await screen.findByText('Deployment summary (1)'));
    expect(await screen.findByText('2 sensors targeted · 1 updated · 1 failed · 0 pending')).toBeVisible();
    await userEvent.click(screen.getByText('View sensor results'));
    expect(screen.getByText('Indoor-AC')).toBeInTheDocument();
    expect(screen.getByText('Outdoor-AC')).toBeInTheDocument();
    expect(screen.getByText(/Outdoor-AC reconnected on 1.2.3 instead of 1.2.4/)).toBeInTheDocument();
    expect(screen.queryByText(/awaiting upload/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Upload release' })).toBeEnabled();

    await userEvent.click(screen.getByRole('button', { name: 'Retry sensor' }));
    await waitFor(() => expect(retryRequest).toMatchObject({
      path: expect.stringContaining('/firmware/deployment-batches/00000000-0000-0000-0000-000000000031/retry'),
      body: { device_ids: ['device-outdoor'] },
    }));
  });
});
