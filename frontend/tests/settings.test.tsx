import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SettingsPage } from '../src/pages/SettingsPage';
import { apiResponse } from './fixtures';
import { installFetchMock, renderWithProviders } from './render';

describe('Settings', () => {
  it('requires the exact typed confirmation before enabling full-account scope', async () => {
    let update: Record<string, unknown> | undefined;
    installFetchMock((path, method, body) => {
      if (path.endsWith('/settings/home-utility') && method === 'PATCH') {
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
    await userEvent.click(await screen.findByRole('button', { name: /Users & access/ }));
    await userEvent.click(screen.getByRole('button', { name: /Alex Morgan/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save access' }));
    expect(await screen.findByText('The server refused this role change.')).toBeInTheDocument();
  });

  it('shows exact system health evidence', async () => {
    installFetchMock();
    renderWithProviders(<SettingsPage />);
    await userEvent.click(await screen.findByRole('button', { name: /Advanced system health/ }));
    expect(screen.getByText('reachable')).toBeInTheDocument();
    expect(screen.getByText(/PZEM ok; storage healthy; backlog 3/)).toBeInTheDocument();
  });
});
