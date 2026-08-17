import { useHeartbeatTickerNow } from '../lib/heartbeatTicker';
import { ABSOLUTE_ISO_TIMESTAMP, formatHeartbeatAge } from '../lib/heartbeatAge';

export function HeartbeatAge({ timestamp }: { timestamp: string | null | undefined }) {
  const tickNow = useHeartbeatTickerNow();
  const text = formatHeartbeatAge(timestamp, tickNow);
  const parsed = timestamp && ABSOLUTE_ISO_TIMESTAMP.test(timestamp) ? Date.parse(timestamp) : Number.NaN;
  if (!Number.isFinite(parsed)) {
    return <span className="heartbeat-age" aria-live="off">Not available</span>;
  }
  const exact = new Date(parsed).toISOString();
  return <time className="heartbeat-age" dateTime={exact} title={`Exact heartbeat: ${exact}`} aria-label={`${text}. Exact heartbeat ${exact}`} aria-live="off">{text}</time>;
}
