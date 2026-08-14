import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, type RenderOptions } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { SessionProvider } from '../src/auth/SessionContext';
import type { Session } from '../src/api/schemas';
import { apiResponse, session } from './fixtures';

export function installFetchMock(handler?: (path: string, method: string, body: BodyInit | null | undefined) => { status: number; body?: unknown; contentType?: string }) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    const method = init?.method ?? 'GET';
    const result = handler?.(path, method, init?.body) ?? apiResponse(path, method);
    const contentType = result.contentType ?? 'application/json';
    const responseBody = result.status === 204 ? null : contentType === 'application/json' ? JSON.stringify(result.body) : typeof result.body === 'string' ? result.body : '';
    return Promise.resolve(new Response(responseBody, {
      status: result.status,
      headers: { 'Content-Type': contentType },
    }));
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

export function renderWithProviders(ui: React.ReactElement, options?: RenderOptions & { currentSession?: Session }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return {
    queryClient,
    ...render(<QueryClientProvider client={queryClient}><SessionProvider session={options?.currentSession ?? session}><MemoryRouter>{ui}</MemoryRouter></SessionProvider></QueryClientProvider>, options),
  };
}
