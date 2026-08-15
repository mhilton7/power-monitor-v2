import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AuthScreen } from '../src/auth/AuthScreen';
import { session } from './fixtures';
import { installFetchMock } from './render';

describe('authentication screens', () => {
  it('submits login credentials through the server session endpoint', async () => {
    const fetchMock = installFetchMock((path) => {
      if (path.endsWith('/auth/login')) return { status: 200, body: { user: session.user } };
      if (path.endsWith('/auth/me')) return { status: 200, body: session.user };
      return { status: 404, body: {} };
    });
    const client = new QueryClient();
    client.setQueryData(['home-scopes'], { home_scopes: [{ id: 'prior-user-home', name: 'Private' }] });
    client.setQueryData(['billing', 'prior-user-home'], { private: 'prior-user-data' });
    render(<QueryClientProvider client={client}><AuthScreen bootstrap={false} /></QueryClientProvider>);
    await userEvent.type(screen.getByLabelText('Email'), 'alex@example.test');
    await userEvent.type(screen.getByLabelText('Password'), 'a-strong-server-password');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const request = fetchMock.mock.calls.find(([url]) => {
      const value = typeof url === 'string' ? url : url instanceof URL ? url.toString() : url.url;
      return value.includes('/auth/login');
    });
    expect(request?.[1]).toMatchObject({ method: 'POST', credentials: 'same-origin' });
    await waitFor(() => {
      expect(client.getQueryData(['home-scopes'])).toBeUndefined();
      expect(client.getQueryData(['billing', 'prior-user-home'])).toBeUndefined();
    });
  });

  it('supports first-run owner setup with a protected password field', () => {
    const client = new QueryClient();
    render(<QueryClientProvider client={client}><AuthScreen bootstrap /></QueryClientProvider>);
    expect(screen.getByRole('heading', { name: 'Create the owner account' })).toBeInTheDocument();
    expect(screen.getByLabelText('Display name')).toBeRequired();
    const password = screen.getByLabelText('Password');
    expect(password).toHaveAttribute('autocomplete', 'new-password');
    fireEvent.change(password, { target: { value: 'short' } });
    expect(password).toHaveAttribute('minlength', '14');
  });
});
