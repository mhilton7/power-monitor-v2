export type TimestampRange = {
  startMs: number;
  endMs: number;
};

export type RangeMode = 'auto' | 'manual';

function orderedFiniteRange(range: TimestampRange, fallback: TimestampRange): TimestampRange {
  if (!Number.isFinite(range.startMs) || !Number.isFinite(range.endMs)) return fallback;
  return range.startMs <= range.endMs
    ? range
    : { startMs: range.endMs, endMs: range.startMs };
}

/**
 * Keeps a timestamp range inside its outer domain without shortening it when
 * either edge crosses the domain boundary. A range wider than the domain is
 * reduced to the complete domain. Invalid input safely falls back to it.
 */
export function clampTimestampRange(
  range: TimestampRange,
  outerDomain: TimestampRange,
  minimumDurationMs = 0,
): TimestampRange {
  const finiteDomain = orderedFiniteRange(outerDomain, { startMs: 0, endMs: 0 });
  const domainDuration = Math.max(0, finiteDomain.endMs - finiteDomain.startMs);
  if (domainDuration === 0) return { ...finiteDomain };

  const finiteRange = orderedFiniteRange(range, finiteDomain);
  const minimumDuration = Math.min(
    domainDuration,
    Math.max(0, Number.isFinite(minimumDurationMs) ? minimumDurationMs : 0),
  );
  const requestedDuration = Math.max(minimumDuration, finiteRange.endMs - finiteRange.startMs);
  const duration = Math.min(domainDuration, requestedDuration);

  let startMs = finiteRange.startMs;
  let endMs = startMs + duration;
  if (startMs < finiteDomain.startMs) {
    startMs = finiteDomain.startMs;
    endMs = startMs + duration;
  }
  if (endMs > finiteDomain.endMs) {
    endMs = finiteDomain.endMs;
    startMs = endMs - duration;
  }

  return { startMs, endMs };
}

/** Moves a range while retaining its duration and clamping at either edge. */
export function moveTimestampRange(
  range: TimestampRange,
  deltaMs: number,
  outerDomain: TimestampRange,
): TimestampRange {
  const current = clampTimestampRange(range, outerDomain);
  const finiteDelta = Number.isFinite(deltaMs) ? deltaMs : 0;
  return clampTimestampRange({
    startMs: current.startMs + finiteDelta,
    endMs: current.endMs + finiteDelta,
  }, outerDomain);
}

export function timestampRangesEqual(
  left: TimestampRange | null | undefined,
  right: TimestampRange | null | undefined,
): boolean {
  if (left === right) return true;
  if (!left || !right) return false;
  return left.startMs === right.startMs && left.endMs === right.endMs;
}
