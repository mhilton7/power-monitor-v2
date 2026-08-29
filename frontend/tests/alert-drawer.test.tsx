import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AlertDrawer } from '../src/components/AlertDrawer';
import { alerts, apiResponse } from './fixtures';
import { installFetchMock, renderWithProviders } from './render';

describe('Alert notification dismissal', () => {
  it('removes one notification and can clear the remaining list', async () => {
    let visible = structuredClone(alerts.alerts);
    const fetchMock = installFetchMock((path, method) => {
      if (path.endsWith('/alerts') && method === 'GET') return { status: 200, body: { alerts: visible } };
      if (path.endsWith('/alerts/notifications') && method === 'DELETE') {
        const dismissedCount = visible.length;
        visible = [];
        return { status: 200, body: { dismissed_count: dismissedCount } };
      }
      if (path.endsWith('/notification') && method === 'DELETE') {
        const id = decodeURIComponent(path.split('/').at(-2) ?? '');
        visible = visible.filter((alert) => alert.id !== id);
        return { status: 200, body: { id, dismissed_at: '2026-08-29T17:00:00Z' } };
      }
      return apiResponse(path, method);
    });
    renderWithProviders(<AlertDrawer open onClose={() => undefined} />);

    expect(await screen.findByRole('heading', { name: 'sensor delivery delayed' })).toBeVisible();
    expect(screen.getByText('2 notifications')).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Remove sensor delivery delayed notification' }));
    expect(screen.getByText(/only from your account/)).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Remove notification' }));
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'sensor delivery delayed' })).not.toBeInTheDocument());
    expect(screen.getByText('1 notification')).toBeVisible();

    await userEvent.click(screen.getByRole('button', { name: 'Clear all' }));
    expect(screen.getByText(/Alert evidence and lifecycle history remain recorded/)).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Clear all' }));
    expect(await screen.findByText('No alerts')).toBeVisible();
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/alerts/notifications',
      expect.objectContaining({ method: 'DELETE' }),
    );
  });
});
