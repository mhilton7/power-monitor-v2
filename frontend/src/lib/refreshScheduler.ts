// Coalesce a burst, but never postpone work forever under sustained telemetry.
// A dirty notification received during an in-flight refresh schedules one more
// pass after completion instead of cancelling/restarting the valid request.
export function createRefreshScheduler(refresh: () => Promise<unknown>, delayMs: number, maximumWaitMs: number) {
  let timer: number | undefined;
  let firstPendingAt: number | undefined;
  let running = false;
  let disposed = false;

  const arm = () => {
    if (disposed || running || firstPendingAt === undefined) return;
    if (timer !== undefined) window.clearTimeout(timer);
    timer = window.setTimeout(flush, Math.max(0, Math.min(delayMs, firstPendingAt + maximumWaitMs - Date.now())));
  };
  const flush = () => {
    timer = undefined;
    if (disposed || running || firstPendingAt === undefined) return;
    firstPendingAt = undefined;
    running = true;
    void refresh().catch(() => undefined).finally(() => {
      running = false;
      arm();
    });
  };
  return {
    schedule() {
      if (disposed) return;
      firstPendingAt ??= Date.now();
      arm();
    },
    dispose() {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    },
  };
}
