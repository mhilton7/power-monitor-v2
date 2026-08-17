const MAX_SMALL_CLOCK_SKEW_MS = 5 * 60 * 1_000;
export const ABSOLUTE_ISO_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

export function formatHeartbeatAge(timestamp: string | null | undefined, nowMs: number): string {
  if (!timestamp || !ABSOLUTE_ISO_TIMESTAMP.test(timestamp)) return 'Not available';
  const timestampMs = Date.parse(timestamp);
  if (!Number.isFinite(timestampMs)) return 'Not available';
  const futureOffset = timestampMs - nowMs;
  if (futureOffset > MAX_SMALL_CLOCK_SKEW_MS) return 'Not available';
  const totalSeconds = Math.max(0, Math.floor((nowMs - timestampMs) / 1_000));
  if (totalSeconds < 60) return `${totalSeconds}s ago`;
  const totalMinutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (totalMinutes < 60) return `${totalMinutes}m ${pad(seconds)}s ago`;
  const totalHours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (totalHours < 24) return `${totalHours}h ${pad(minutes)}m ${pad(seconds)}s ago`;
  const days = Math.floor(totalHours / 24);
  const hours = totalHours % 24;
  return `${days}d ${pad(hours)}h ${pad(minutes)}m ago`;
}
