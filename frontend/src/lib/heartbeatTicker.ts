import { useSyncExternalStore } from 'react';

type Listener = () => void;

const listeners = new Set<Listener>();
let currentNow = Date.now();
let intervalId: number | null = null;

function publishCurrentTime() {
  currentNow = Date.now();
  for (const listener of listeners) listener();
}

function handleVisibilityChange() {
  if (document.visibilityState === 'visible') publishCurrentTime();
}

function handleFocus() {
  publishCurrentTime();
}

function startTicker() {
  currentNow = Date.now();
  intervalId = window.setInterval(publishCurrentTime, 1_000);
  document.addEventListener('visibilitychange', handleVisibilityChange);
  window.addEventListener('focus', handleFocus);
}

function stopTicker() {
  if (intervalId !== null) window.clearInterval(intervalId);
  intervalId = null;
  document.removeEventListener('visibilitychange', handleVisibilityChange);
  window.removeEventListener('focus', handleFocus);
}

function subscribe(listener: Listener) {
  listeners.add(listener);
  if (listeners.size === 1) startTicker();
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) stopTicker();
  };
}

function getSnapshot() {
  return currentNow;
}

export function useHeartbeatTickerNow(): number {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
