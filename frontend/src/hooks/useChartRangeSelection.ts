import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  clampTimestampRange,
  timestampRangesEqual,
  type RangeMode,
  type TimestampRange,
} from '../lib/chartRange';

interface RangeSelectionState {
  identity: string;
  mode: RangeMode;
  manualSelection: TimestampRange | null;
  clampEvent: { key: string; selection: TimestampRange } | null;
}

interface RangeSelectionOptions {
  identity: string;
  outerDomain: TimestampRange;
  minimumDurationMs?: number;
  onClamp?: (selection: TimestampRange) => void;
}

export function useChartRangeSelection({
  identity,
  outerDomain,
  minimumDurationMs = 1,
  onClamp,
}: RangeSelectionOptions) {
  const [state, setState] = useState<RangeSelectionState>(() => ({
    identity,
    mode: 'auto',
    manualSelection: null,
    clampEvent: null,
  }));
  const lastClampNotification = useRef<string | null>(null);

  // Identity is an explicit product transition (home, scope, metric, or
  // preset), not a data lifecycle event. Adjusting here prevents a previous
  // identity's manual range from being committed or briefly painted.
  if (state.identity !== identity) {
    setState({ identity, mode: 'auto', manualSelection: null, clampEvent: null });
  }

  const currentState = state.identity === identity
    ? state
    : { identity, mode: 'auto' as const, manualSelection: null, clampEvent: null };
  const boundedManualSelection = useMemo(() => currentState.manualSelection
    ? clampTimestampRange(currentState.manualSelection, outerDomain, minimumDurationMs)
    : null, [currentState.manualSelection, minimumDurationMs, outerDomain]);

  if (currentState.mode === 'manual'
    && currentState.manualSelection
    && boundedManualSelection
    && !timestampRangesEqual(currentState.manualSelection, boundedManualSelection)) {
    const key = `${identity}:${currentState.manualSelection.startMs}:${currentState.manualSelection.endMs}:${boundedManualSelection.startMs}:${boundedManualSelection.endMs}`;
    setState({ identity, mode: 'manual', manualSelection: boundedManualSelection, clampEvent: { key, selection: boundedManualSelection } });
  }

  useEffect(() => {
    if (!currentState.clampEvent) {
      lastClampNotification.current = null;
      return;
    }
    if (lastClampNotification.current === currentState.clampEvent.key) return;
    lastClampNotification.current = currentState.clampEvent.key;
    onClamp?.(currentState.clampEvent.selection);
  }, [currentState.clampEvent, onClamp]);

  const beginManual = useCallback((selection: TimestampRange) => {
    setState({
      identity,
      mode: 'manual',
      manualSelection: clampTimestampRange(selection, outerDomain, minimumDurationMs),
      clampEvent: null,
    });
  }, [identity, minimumDurationMs, outerDomain]);

  const commitManual = useCallback((selection: TimestampRange) => {
    setState({
      identity,
      mode: 'manual',
      manualSelection: clampTimestampRange(selection, outerDomain, minimumDurationMs),
      clampEvent: null,
    });
  }, [identity, minimumDurationMs, outerDomain]);

  const reset = useCallback(() => {
    setState({ identity, mode: 'auto', manualSelection: null, clampEvent: null });
  }, [identity]);

  return {
    mode: currentState.mode,
    manualSelection: boundedManualSelection,
    selection: boundedManualSelection ?? outerDomain,
    beginManual,
    commitManual,
    reset,
  };
}
