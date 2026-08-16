import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SettingsPage } from '../src/pages/SettingsPage';
import { useHomeScope } from '../src/home/useHomeScope';
import { apiResponse, homeUtility } from './fixtures';
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
    await userEvent.click(screen.getByRole('checkbox', { name: /Eligible for aggregates/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save sensor' }));
    await waitFor(() => expect(update).toMatchObject({
      location: 'Garage panel', notes: 'Verified one-CT sensor', display_order: 4,
      include_in_aggregate: false, show_on_dashboard: true, monitoring_enabled: true,
    }));
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
    installFetchMock();
    renderWithProviders(<SettingsPage />);
    await userEvent.click(await screen.findByRole('button', { name: /Advanced system health/ }));
    expect(screen.getByText('reachable')).toBeInTheDocument();
    expect(screen.getByText(/PZEM ok; storage healthy; backlog 3/)).toBeInTheDocument();
  });
});
