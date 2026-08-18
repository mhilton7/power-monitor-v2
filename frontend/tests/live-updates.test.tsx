import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, waitFor } from '@testing-library/react';
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

  close(): void {}
}

function Harness() {
  useLiveUpdates();
  return null;
}

describe('Live query refresh', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('invalidates History when the server accepts a measurement', async () => {
    vi.stubGlobal('EventSource', CapturedEventSource);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
    render(<QueryClientProvider client={queryClient}><Harness /></QueryClientProvider>);

    CapturedEventSource.latest?.dispatchEvent(new Event('measurement'));

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['history'] }));
  });
});
