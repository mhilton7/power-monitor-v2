import { useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { eventSource } from '../api/client';
import { createRefreshScheduler } from '../lib/refreshScheduler';

const LIVE_QUERY_KEYS = [['home'], ['live-pricing'], ['alerts'], ['devices']] as const;
export const HISTORY_RECOVERY_MS = 30_000;

export function useLiveUpdates(): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    let polling: number | undefined;
    let disconnected = false;
    const source = eventSource();
    const invalidate = (keys: readonly (readonly string[])[]) => Promise.all(keys.map((queryKey) => queryClient.invalidateQueries({ queryKey }, { cancelRefetch: false })));
    const live = createRefreshScheduler(() => invalidate(LIVE_QUERY_KEYS), 100, 500);
    const history = createRefreshScheduler(() => invalidate([['history'], ['billing']]), 750, 3_000);
    const visible = () => document.visibilityState !== 'hidden';
    const refresh = () => { if (visible()) live.schedule(); };
    const recover = () => {
      if (!visible()) return;
      live.schedule();
      history.schedule();
    };
    const acceptedMeasurement = () => {
      refresh();
      // Current deployments have no range/revision payload. Refresh all active
      // history scopes conservatively, retaining late/backfilled notifications
      // regardless of their timestamps rather than keeping only the last one.
      if (visible()) history.schedule();
    };
    source.addEventListener('measurement', acceptedMeasurement);
    source.addEventListener('measurement_accepted', acceptedMeasurement);
    source.addEventListener('accepted_reading', acceptedMeasurement);
    source.addEventListener('heartbeat', refresh);
    source.addEventListener('alert', refresh);
    source.addEventListener('command', refresh);
    source.addEventListener('rate', recover);
    source.addEventListener('refresh', refresh);
    // The server's five-second `refresh` is a heartbeat, not measurement
    // evidence. History still catches new buckets/backfills without SSE data.
    const recovery = window.setInterval(() => { if (visible()) history.schedule(); }, HISTORY_RECOVERY_MS);
    source.onopen = () => {
      if (polling !== undefined) window.clearInterval(polling);
      polling = undefined;
      if (disconnected) recover();
      disconnected = false;
    };
    source.onerror = () => {
      // EventSource owns reconnection. Closing it here permanently stopped SSE.
      disconnected = true;
      if (polling === undefined) polling = window.setInterval(refresh, 15_000);
    };
    window.addEventListener('focus', recover);
    window.addEventListener('online', recover);
    document.addEventListener('visibilitychange', recover);
    return () => {
      source.close();
      live.dispose();
      history.dispose();
      window.clearInterval(recovery);
      if (polling !== undefined) window.clearInterval(polling);
      window.removeEventListener('focus', recover);
      window.removeEventListener('online', recover);
      document.removeEventListener('visibilitychange', recover);
    };
  }, [queryClient]);
}
