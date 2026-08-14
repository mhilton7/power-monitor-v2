import type { z } from 'zod';

export const API_BASE = '/api/v1';

export interface ProblemDetails {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  instance?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly problem: ProblemDetails;

  constructor(status: number, problem: ProblemDetails) {
    super(problem.detail ?? problem.title ?? `Request failed (${status})`);
    this.name = 'ApiError';
    this.status = status;
    this.problem = problem;
  }
}

function csrfToken(): string | undefined {
  const cookie = document.cookie
    .split('; ')
    .find((entry) => entry.startsWith('pm_csrf='));
  return cookie ? decodeURIComponent(cookie.slice('pm_csrf='.length)) : undefined;
}

async function parseProblem(response: Response): Promise<ProblemDetails> {
  if (response.headers.get('content-type')?.toLowerCase().includes('json')) {
    return await response.json() as ProblemDetails;
  }
  return { status: response.status, title: response.statusText };
}

export async function apiRequest<T>(
  path: string,
  schema: z.ZodType<T>,
  init: RequestInit = {},
): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase();
  const headers = new Headers(init.headers);
  headers.set('Accept', 'application/json');
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const token = csrfToken();
    if (token) headers.set('X-CSRF-Token', token);
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    method,
    headers,
    credentials: 'same-origin',
    redirect: 'error',
  });

  if (!response.ok) {
    const problem = await parseProblem(response);
    if (response.status === 401 && path !== '/auth/me') window.dispatchEvent(new CustomEvent('pm:session-expired'));
    throw new ApiError(response.status, problem);
  }

  if (response.status === 204) return schema.parse(undefined);
  return schema.parse(await response.json());
}

export function jsonBody(value: unknown): string {
  return JSON.stringify(value);
}

export function eventSource(path = '/events'): EventSource {
  return new EventSource(`${API_BASE}${path}`, { withCredentials: true });
}

export function isForbidden(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}
