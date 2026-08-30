import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { ChartRangeSelector } from '../src/components/ChartRangeSelector';
import {
  clampTimestampRange,
  moveTimestampRange,
  timestampRangesEqual,
  type RangeMode,
  type TimestampRange,
} from '../src/lib/chartRange';

const outerDomain = { startMs: 0, endMs: 1_000 };
const initialSelection = { startMs: 200, endMs: 800 };

function ControlledSelector({
  domain = outerDomain,
  initial = initialSelection,
  onManualStart = vi.fn(),
  onCommit = vi.fn(),
}: {
  domain?: TimestampRange;
  initial?: TimestampRange;
  onManualStart?: (range: TimestampRange) => void;
  onCommit?: (range: TimestampRange) => void;
}) {
  const [selection, setSelection] = useState(initial);
  const [mode, setMode] = useState<RangeMode>('auto');
  return <ChartRangeSelector
    label="Power History range"
    outerDomain={domain}
    selection={selection}
    mode={mode}
    minimumDurationMs={10}
    formatValue={(value) => `${value} ms`}
    onManualStart={(range) => {
      onManualStart(range);
      setMode('manual');
    }}
    onCommit={(range) => {
      onCommit(range);
      setSelection(range);
    }}
    testId="power-range"
  />;
}

function setTrackWidth(width = 1_000) {
  const track = screen.getByTestId('power-range-track');
  vi.spyOn(track, 'getBoundingClientRect').mockReturnValue({
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    right: width,
    bottom: 44,
    width,
    height: 44,
    toJSON: () => ({}),
  });
}

describe('timestamp chart ranges', () => {
  it('clamps at either edge while preserving duration', () => {
    expect(clampTimestampRange({ startMs: -100, endMs: 300 }, outerDomain)).toEqual({ startMs: 0, endMs: 400 });
    expect(clampTimestampRange({ startMs: 800, endMs: 1_200 }, outerDomain)).toEqual({ startMs: 600, endMs: 1_000 });
    expect(clampTimestampRange({ startMs: 900, endMs: 905 }, outerDomain, 100)).toEqual({ startMs: 900, endMs: 1_000 });
    expect(clampTimestampRange({ startMs: -100, endMs: 1_200 }, outerDomain)).toEqual(outerDomain);
  });

  it('moves a range without changing its duration', () => {
    expect(moveTimestampRange(initialSelection, 100, outerDomain)).toEqual({ startMs: 300, endMs: 900 });
    expect(moveTimestampRange(initialSelection, 500, outerDomain)).toEqual({ startMs: 400, endMs: 1_000 });
    expect(moveTimestampRange(initialSelection, -500, outerDomain)).toEqual({ startMs: 0, endMs: 600 });
    expect(timestampRangesEqual(initialSelection, { ...initialSelection })).toBe(true);
    expect(timestampRangesEqual(initialSelection, { startMs: 201, endMs: 800 })).toBe(false);
    expect(timestampRangesEqual(null, undefined)).toBe(false);
  });
});

describe('ChartRangeSelector', () => {
  it('keeps pointer changes local until release and commits the left edge once', () => {
    const onManualStart = vi.fn();
    const onCommit = vi.fn();
    render(<ControlledSelector onManualStart={onManualStart} onCommit={onCommit} />);
    setTrackWidth();
    const start = screen.getByTestId('power-range-start');

    fireEvent.pointerDown(start, { pointerId: 1, button: 0, clientX: 200 });
    fireEvent.pointerMove(start, { pointerId: 1, clientX: 300 });

    expect(onManualStart).toHaveBeenCalledTimes(1);
    expect(onManualStart).toHaveBeenCalledWith({ startMs: 200, endMs: 800 });
    expect(onCommit).not.toHaveBeenCalled();
    expect(start).toHaveAttribute('aria-valuenow', '300');
    expect(screen.getByTestId('power-range-status')).toHaveTextContent('200 ms to 800 ms');

    fireEvent.pointerUp(start, { pointerId: 1, clientX: 300 });

    expect(onManualStart).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenLastCalledWith({ startMs: 300, endMs: 800 });
    expect(screen.getByTestId('power-range-status')).toHaveTextContent('300 ms to 800 ms');
  });

  it('resizes the right edge and moves the whole selected window', () => {
    const onCommit = vi.fn();
    render(<ControlledSelector onCommit={onCommit} />);
    setTrackWidth();
    const end = screen.getByTestId('power-range-end');

    fireEvent.pointerDown(end, { pointerId: 2, button: 0, clientX: 800 });
    fireEvent.pointerMove(end, { pointerId: 2, clientX: 700 });
    fireEvent.pointerUp(end, { pointerId: 2, clientX: 700 });
    expect(onCommit).toHaveBeenLastCalledWith({ startMs: 200, endMs: 700 });

    const windowControl = screen.getByTestId('power-range-window');
    fireEvent.pointerDown(windowControl, { pointerId: 3, button: 0, clientX: 450 });
    fireEvent.pointerMove(windowControl, { pointerId: 3, clientX: 550 });
    expect(onCommit).toHaveBeenCalledTimes(1);
    fireEvent.pointerUp(windowControl, { pointerId: 3, clientX: 550 });

    expect(onCommit).toHaveBeenCalledTimes(2);
    expect(onCommit).toHaveBeenLastCalledWith({ startMs: 300, endMs: 800 });
    expect(screen.getByTestId('power-range-window')).toHaveAttribute('aria-valuetext', '300 ms to 800 ms');
  });

  it('supports arrows, paging, Home, and End on thumbs and the selected window', () => {
    const onCommit = vi.fn();
    render(<ControlledSelector onCommit={onCommit} />);
    const start = screen.getByTestId('power-range-start');
    const end = screen.getByTestId('power-range-end');
    const windowControl = screen.getByTestId('power-range-window');

    fireEvent.keyDown(start, { key: 'ArrowRight' });
    expect(onCommit).toHaveBeenLastCalledWith({ startMs: 210, endMs: 800 });
    fireEvent.keyDown(end, { key: 'PageDown' });
    expect(onCommit).toHaveBeenLastCalledWith({ startMs: 210, endMs: 700 });
    fireEvent.keyDown(windowControl, { key: 'PageUp' });
    expect(onCommit).toHaveBeenLastCalledWith({ startMs: 310, endMs: 800 });
    fireEvent.keyDown(windowControl, { key: 'End' });
    expect(onCommit).toHaveBeenLastCalledWith({ startMs: 510, endMs: 1_000 });
    fireEvent.keyDown(start, { key: 'Home' });
    expect(onCommit).toHaveBeenLastCalledWith({ startMs: 0, endMs: 1_000 });
    fireEvent.keyDown(end, { key: 'Home' });
    expect(onCommit).toHaveBeenLastCalledWith({ startMs: 0, endMs: 10 });
  });

  it('preserves committed timestamps across rerenders and an expanded data domain', () => {
    const { rerender } = render(<ControlledSelector />);
    expect(screen.getByTestId('power-range-status')).toHaveTextContent('200 ms to 800 ms');

    rerender(<ControlledSelector domain={{ startMs: -500, endMs: 1_500 }} />);

    expect(screen.getByTestId('power-range-start')).toHaveAttribute('aria-valuenow', '200');
    expect(screen.getByTestId('power-range-end')).toHaveAttribute('aria-valuenow', '800');
    expect(screen.getByTestId('power-range-status')).toHaveTextContent('200 ms to 800 ms');
  });

  it('cancels interrupted pointer input, clears the draft, and accepts the next gesture', () => {
    const onManualStart = vi.fn();
    const onCommit = vi.fn();
    render(<ControlledSelector onManualStart={onManualStart} onCommit={onCommit} />);
    setTrackWidth();
    const start = screen.getByTestId('power-range-start');

    fireEvent.pointerDown(start, { pointerId: 4, button: 0, clientX: 200 });
    fireEvent.pointerMove(start, { pointerId: 4, clientX: 350 });
    expect(start).toHaveAttribute('aria-valuenow', '350');
    expect(onManualStart).toHaveBeenCalledTimes(1);
    fireEvent.pointerCancel(start, { pointerId: 4 });
    expect(onManualStart).toHaveBeenCalledTimes(1);
    expect(onCommit).not.toHaveBeenCalled();
    expect(start).toHaveAttribute('aria-valuenow', '200');

    fireEvent.pointerDown(start, { pointerId: 5, button: 0, clientX: 200 });
    fireEvent.pointerMove(start, { pointerId: 5, clientX: 250 });
    fireEvent.pointerUp(start, { pointerId: 5, clientX: 250 });
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledWith({ startMs: 250, endMs: 800 });
  });

  it('keeps the interaction domain stable when live data advances during a drag', () => {
    const onManualStart = vi.fn();
    const onCommit = vi.fn();
    const view = render(<ControlledSelector onManualStart={onManualStart} onCommit={onCommit} />);
    setTrackWidth();
    const start = screen.getByTestId('power-range-start');

    fireEvent.pointerDown(start, { pointerId: 8, button: 0, clientX: 200 });
    fireEvent.pointerMove(start, { pointerId: 8, clientX: 300 });
    expect(onManualStart).toHaveBeenCalledTimes(1);
    expect(start).toHaveAttribute('aria-valuenow', '300');

    view.rerender(<ControlledSelector
      domain={{ startMs: 100, endMs: 1_100 }}
      onManualStart={onManualStart}
      onCommit={onCommit}
    />);
    expect(start).toHaveAttribute('aria-valuenow', '300');

    fireEvent.pointerMove(start, { pointerId: 8, clientX: 400 });
    fireEvent.pointerUp(start, { pointerId: 8, clientX: 400 });
    expect(onCommit).toHaveBeenCalledWith({ startMs: 400, endMs: 800 });
  });

  it('keeps auto mode after a no-motion tap and exposes non-overlapping thumb hit targets', () => {
    const onManualStart = vi.fn();
    const onCommit = vi.fn();
    render(<ControlledSelector initial={{ startMs: 495, endMs: 505 }} onManualStart={onManualStart} onCommit={onCommit} />);
    setTrackWidth();
    const start = screen.getByTestId('power-range-start');
    const end = screen.getByTestId('power-range-end');

    fireEvent.pointerDown(start, { pointerId: 6, button: 0, clientX: 495 });
    fireEvent.pointerUp(start, { pointerId: 6, clientX: 495 });

    expect(onManualStart).not.toHaveBeenCalled();
    expect(onCommit).not.toHaveBeenCalled();
    expect(screen.getByText('Following full range')).toBeInTheDocument();
    expect(getComputedStyle(start).zIndex).toBe(getComputedStyle(end).zIndex);
    expect(start).toHaveClass('chart-range-selector__thumb--start');
    expect(end).toHaveClass('chart-range-selector__thumb--end');
  });
});
