import { act, renderHook } from '@testing-library/react';
import { useEffect, useState } from 'react';
import { useChartRangeSelection } from '../src/hooks/useChartRangeSelection';
import type { TimestampRange } from '../src/lib/chartRange';

type HookProps = {
  identity: string;
  outerDomain: TimestampRange;
  minimumDurationMs?: number;
  onClamp?: (selection: TimestampRange) => void;
};

function useSelection(props: HookProps) {
  return useChartRangeSelection(props);
}

function useTickingSelection(props: HookProps) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((value) => value + 1), 1_000);
    return () => window.clearInterval(timer);
  }, []);
  return { ...useChartRangeSelection(props), tick };
}

describe('useChartRangeSelection', () => {
  it('preserves a timestamp-owned manual selection across rerenders and domain expansion', () => {
    const { result, rerender } = renderHook(useSelection, {
      initialProps: {
        identity: 'home-a:power:24-hours',
        outerDomain: { startMs: 0, endMs: 1_000 },
      },
    });

    act(() => result.current.commitManual({ startMs: 200, endMs: 700 }));
    expect(result.current.mode).toBe('manual');
    expect(result.current.manualSelection).toEqual({ startMs: 200, endMs: 700 });

    rerender({
      identity: 'home-a:power:24-hours',
      outerDomain: { startMs: -500, endMs: 1_500 },
    });

    expect(result.current.mode).toBe('manual');
    expect(result.current.manualSelection).toEqual({ startMs: 200, endMs: 700 });
    expect(result.current.selection).toEqual({ startMs: 200, endMs: 700 });
  });

  it('preserves a manual timestamp range through one and ten clock ticks with fake timers', () => {
    vi.useFakeTimers();
    const { result, unmount } = renderHook(useTickingSelection, {
      initialProps: {
        identity: 'home-a:power:24-hours',
        outerDomain: { startMs: 0, endMs: 86_400_000 },
      },
    });

    act(() => result.current.commitManual({ startMs: 21_600_000, endMs: 64_800_000 }));
    const selected = result.current.selection;
    act(() => { vi.advanceTimersByTime(1_000); });
    expect(result.current.tick).toBe(1);
    expect(result.current.selection).toEqual(selected);
    act(() => { vi.advanceTimersByTime(9_000); });
    expect(result.current.tick).toBe(10);
    expect(result.current.selection).toEqual(selected);

    unmount();
    vi.useRealTimers();
  });

  it('resets to the new full domain when and only when the selection identity changes', () => {
    const { result, rerender } = renderHook(useSelection, {
      initialProps: {
        identity: 'home-a:power:24-hours',
        outerDomain: { startMs: 0, endMs: 1_000 },
      },
    });

    act(() => result.current.beginManual({ startMs: 250, endMs: 750 }));
    rerender({
      identity: 'home-a:power:24-hours',
      outerDomain: { startMs: 0, endMs: 1_000 },
    });
    expect(result.current.mode).toBe('manual');
    expect(result.current.selection).toEqual({ startMs: 250, endMs: 750 });

    rerender({
      identity: 'home-a:power:7-days',
      outerDomain: { startMs: -6_000, endMs: 1_000 },
    });
    expect(result.current.mode).toBe('auto');
    expect(result.current.manualSelection).toBeNull();
    expect(result.current.selection).toEqual({ startMs: -6_000, endMs: 1_000 });
  });

  it('clamps a manual range to a shrinking domain while preserving duration when possible', () => {
    const onClamp = vi.fn();
    const { result, rerender } = renderHook(useSelection, {
      initialProps: {
        identity: 'home-a:power:24-hours',
        outerDomain: { startMs: 0, endMs: 1_000 },
        onClamp,
      },
    });

    act(() => result.current.commitManual({ startMs: 100, endMs: 500 }));
    rerender({
      identity: 'home-a:power:24-hours',
      outerDomain: { startMs: 300, endMs: 900 },
      onClamp,
    });

    expect(result.current.selection).toEqual({ startMs: 300, endMs: 700 });
    expect(result.current.selection.endMs - result.current.selection.startMs).toBe(400);
    expect(onClamp).toHaveBeenLastCalledWith({ startMs: 300, endMs: 700 });

    rerender({
      identity: 'home-a:power:24-hours',
      outerDomain: { startMs: 350, endMs: 600 },
      onClamp,
    });

    expect(result.current.selection).toEqual({ startMs: 350, endMs: 600 });
    expect(onClamp).toHaveBeenLastCalledWith({ startMs: 350, endMs: 600 });

    rerender({
      identity: 'home-a:power:24-hours',
      outerDomain: { startMs: 0, endMs: 1_000 },
      onClamp,
    });

    expect(result.current.selection).toEqual({ startMs: 350, endMs: 600 });
    expect(result.current.manualSelection).toEqual({ startMs: 350, endMs: 600 });
  });

  it('does not reset manual mode when equivalent same-identity inputs get new references', () => {
    const { result, rerender } = renderHook(useSelection, {
      initialProps: {
        identity: 'home-a:power:24-hours',
        outerDomain: { startMs: 0, endMs: 1_000 },
        minimumDurationMs: 25,
      },
    });

    act(() => result.current.commitManual({ startMs: 125, endMs: 625 }));
    rerender({
      identity: 'home-a:power:24-hours',
      outerDomain: { startMs: 0, endMs: 1_000 },
      minimumDurationMs: 25,
    });

    expect(result.current.mode).toBe('manual');
    expect(result.current.manualSelection).toEqual({ startMs: 125, endMs: 625 });
  });

  it('returns to automatic full-domain selection on explicit reset', () => {
    const outerDomain = { startMs: 0, endMs: 1_000 };
    const { result } = renderHook(useSelection, {
      initialProps: { identity: 'home-a:power:24-hours', outerDomain },
    });

    act(() => result.current.commitManual({ startMs: 200, endMs: 800 }));
    expect(result.current.mode).toBe('manual');

    act(() => result.current.reset());

    expect(result.current.mode).toBe('auto');
    expect(result.current.manualSelection).toBeNull();
    expect(result.current.selection).toEqual(outerDomain);
  });
});
