import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, waitFor } from '@testing-library/react';
import { useLiveUpdates } from '../src/hooks/useLiveUpdates';

class CapturedEventSource extends EventTarget {
  static latest: CapturedEventSource | undefined;
  readonly url: string;
  readonly withCredentials = true;
  readonly readyState = 1;
  onerror: ((event: Event) => unknown) | null = null;
  onmessage: ((event: MessageEvent) => unknown) | null = null;
  onopen: ((event: Event) => unknown) | null = null;

  constructor(url: string | URL) {
    super();
    this.url = String(url);
    CapturedEventSource.latest = this;
  }

  close = vi.fn();
}

function Harness() {
  useLiveUpdates();
  return null;
}

describe('Live query refresh', () => {
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it('invalidates History when the server accepts a measurement', async () => {
    vi.stubGlobal('EventSource', CapturedEventSource);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
    render(<QueryClientProvider client={queryClient}><Harness /></QueryClientProvider>);

    CapturedEventSource.latest?.dispatchEvent(new Event('accepted_reading'));

    await waitFor(() => expect(invalidate.mock.calls.some(([filters]) => filters?.queryKey?.[0] === 'history')).toBe(true));
  });

  it('does not reload histories or billing on routine server refresh/heartbeat messages', async () => {
    vi.stubGlobal('EventSource', CapturedEventSource);
    const queryClient = new QueryClient();
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
    render(<QueryClientProvider client={queryClient}><Harness /></QueryClientProvider>);
    CapturedEventSource.latest?.dispatchEvent(new Event('refresh'));
    CapturedEventSource.latest?.dispatchEvent(new Event('heartbeat'));
    await waitFor(() => expect(invalidate).toHaveBeenCalled());
    expect(invalidate.mock.calls.filter(([filters]) => ['history', 'billing'].includes(String(filters?.queryKey?.[0])))).toHaveLength(0);
  });

  it('coalesces bursts and sustained traffic without cancelling an active snapshot', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('EventSource', CapturedEventSource);
    const queryClient = new QueryClient();
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
    render(<QueryClientProvider client={queryClient}><Harness /></QueryClientProvider>);
    const historyCalls = () => invalidate.mock.calls.filter(([filters]) => filters?.queryKey?.[0] === 'history');
    for (let index = 0; index < 20; index += 1) CapturedEventSource.latest?.dispatchEvent(new Event('accepted_reading'));
    expect(historyCalls()).toHaveLength(0);
    await act(() => vi.advanceTimersByTimeAsync(750));
    expect(historyCalls()).toHaveLength(1);
    for (let index = 0; index < 40; index += 1) {
      CapturedEventSource.latest?.dispatchEvent(new Event('accepted_reading'));
      await act(() => vi.advanceTimersByTimeAsync(100));
    }
    expect(historyCalls().length).toBeGreaterThanOrEqual(2);
    expect(historyCalls().length).toBeLessThanOrEqual(3);
    expect(historyCalls().every(([, options]) => options?.cancelRefetch === false)).toBe(true);
  });

  it('keeps native SSE reconnection, bounded recovery and cleans up listeners/timers', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('EventSource', CapturedEventSource);
    const queryClient = new QueryClient();
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
    const view = render(<QueryClientProvider client={queryClient}><Harness /></QueryClientProvider>);
    const source = CapturedEventSource.latest!;
    source.onerror?.(new Event('error'));
    expect(source.close).not.toHaveBeenCalled();
    await act(() => vi.advanceTimersByTimeAsync(30_750));
    expect(invalidate.mock.calls.some(([filters]) => filters?.queryKey?.[0] === 'history')).toBe(true);
    source.onopen?.(new Event('open'));
    await act(() => vi.advanceTimersByTimeAsync(750));
    view.unmount();
    const calls = invalidate.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    window.dispatchEvent(new Event('focus'));
    expect(invalidate).toHaveBeenCalledTimes(calls);
    expect(source.close).toHaveBeenCalledTimes(1);
  });

  it('retains dirty evidence from out-of-order/backfilled readings across all cached history scopes', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('EventSource', CapturedEventSource);
    const queryClient = new QueryClient();
    queryClient.setQueryData(['history', 'home-a', 'old-range'], { points: [] });
    queryClient.setQueryData(['history', 'home-a', 'new-range'], { points: [] });
    render(<QueryClientProvider client={queryClient}><Harness /></QueryClientProvider>);
    CapturedEventSource.latest?.dispatchEvent(new MessageEvent('measurement_accepted', { data: JSON.stringify({ timestamp: '2026-09-29T00:00:00Z' }) }));
    CapturedEventSource.latest?.dispatchEvent(new MessageEvent('measurement_accepted', { data: JSON.stringify({ timestamp: '2026-08-01T00:00:00Z' }) }));
    await act(() => vi.advanceTimersByTimeAsync(750));
    expect(queryClient.getQueryState(['history', 'home-a', 'old-range'])?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(['history', 'home-a', 'new-range'])?.isInvalidated).toBe(true);
  });

  it('pauses hidden-tab rendering requests and recovers history once visible', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('EventSource', CapturedEventSource);
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    const queryClient = new QueryClient();
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
    render(<QueryClientProvider client={queryClient}><Harness /></QueryClientProvider>);
    CapturedEventSource.latest?.dispatchEvent(new Event('accepted_reading'));
    await act(() => vi.advanceTimersByTimeAsync(31_000));
    expect(invalidate).not.toHaveBeenCalled();
    visibility.mockReturnValue('visible');
    document.dispatchEvent(new Event('visibilitychange'));
    await act(() => vi.advanceTimersByTimeAsync(750));
    expect(invalidate.mock.calls.filter(([filters]) => filters?.queryKey?.[0] === 'history')).toHaveLength(1);
    visibility.mockRestore();
  });
});
