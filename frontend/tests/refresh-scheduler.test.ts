import { createRefreshScheduler } from '../src/lib/refreshScheduler';

describe('bounded refresh scheduling', () => {
  afterEach(() => vi.useRealTimers());

  it('retains a dirty notification during a slow response without parallel or cancelled work', async () => {
    vi.useFakeTimers();
    let complete: (() => void) | undefined;
    const refresh = vi.fn(() => new Promise<void>((resolve) => { complete = resolve; }));
    const scheduler = createRefreshScheduler(refresh, 750, 3_000);
    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(750);
    expect(refresh).toHaveBeenCalledTimes(1);
    for (let index = 0; index < 20; index += 1) scheduler.schedule();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(refresh).toHaveBeenCalledTimes(1);
    complete?.();
    await vi.advanceTimersByTimeAsync(1);
    expect(refresh).toHaveBeenCalledTimes(2);
    complete?.();
    scheduler.dispose();
  });

  it('never starves under continuous events and stops after disposal', async () => {
    vi.useFakeTimers();
    const refresh = vi.fn(() => Promise.resolve());
    const scheduler = createRefreshScheduler(refresh, 750, 3_000);
    for (let index = 0; index < 100; index += 1) {
      scheduler.schedule();
      await vi.advanceTimersByTimeAsync(100);
    }
    expect(refresh).toHaveBeenCalledTimes(3);
    scheduler.dispose();
    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(refresh).toHaveBeenCalledTimes(3);
  });
});
