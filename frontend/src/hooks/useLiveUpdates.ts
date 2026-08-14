import { useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { eventSource } from '../api/client';

const LIVE_QUERY_KEYS = [['home'], ['alerts'], ['devices'], ['billing']] as const;

export function useLiveUpdates(): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    let polling: number | undefined;
    const source = eventSource();
    const refresh = () => {
      for (const key of LIVE_QUERY_KEYS) void queryClient.invalidateQueries({ queryKey: key });
    };
    source.addEventListener('measurement', refresh);
    source.addEventListener('heartbeat', refresh);
    source.addEventListener('alert', refresh);
    source.addEventListener('command', refresh);
    source.addEventListener('rate', refresh);
    source.addEventListener('refresh', refresh);
    source.onerror = () => {
      source.close();
      if (polling === undefined) polling = window.setInterval(refresh, 15_000);
    };
    return () => {
      source.close();
      if (polling !== undefined) window.clearInterval(polling);
    };
  }, [queryClient]);
}
