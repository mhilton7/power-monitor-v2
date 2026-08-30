import { useId, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from 'react';
import {
  clampTimestampRange,
  moveTimestampRange,
  timestampRangesEqual,
  type RangeMode,
  type TimestampRange,
} from '../lib/chartRange';
import './ChartRangeSelector.css';

type RangePart = 'start' | 'end' | 'window';

type PointerInteraction = {
  pointerId: number;
  part: RangePart;
  manualStarted: boolean;
  initialClientX: number;
  initialRange: TimestampRange;
  outerDomain: TimestampRange;
  trackWidth: number;
};

export type ChartRangeSelectorProps = {
  label: string;
  outerDomain: TimestampRange;
  selection: TimestampRange;
  mode: RangeMode;
  minimumDurationMs?: number;
  formatValue: (timestampMs: number) => string;
  onManualStart: (range: TimestampRange) => void;
  onCommit: (range: TimestampRange) => void;
  testId?: string;
};

function bounded(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

function resizeRange(
  range: TimestampRange,
  part: 'start' | 'end',
  nextValue: number,
  outerDomain: TimestampRange,
  minimumDurationMs: number,
): TimestampRange {
  const current = clampTimestampRange(range, outerDomain, minimumDurationMs);
  if (part === 'start') {
    return {
      startMs: bounded(nextValue, outerDomain.startMs, current.endMs - minimumDurationMs),
      endMs: current.endMs,
    };
  }
  return {
    startMs: current.startMs,
    endMs: bounded(nextValue, current.startMs + minimumDurationMs, outerDomain.endMs),
  };
}

function positionPercent(value: number, outerDomain: TimestampRange): number {
  const duration = outerDomain.endMs - outerDomain.startMs;
  if (duration <= 0) return 0;
  return bounded(((value - outerDomain.startMs) / duration) * 100, 0, 100);
}

function pointerCapture(target: HTMLElement, pointerId: number): void {
  try {
    target.setPointerCapture(pointerId);
  } catch {
    // A synthetic event or an interrupted native gesture may not own capture.
  }
}

function releasePointerCapture(target: HTMLElement, pointerId: number): void {
  try {
    if (target.hasPointerCapture(pointerId)) target.releasePointerCapture(pointerId);
  } catch {
    // Capture may already have been released by the browser.
  }
}

export function ChartRangeSelector({
  label,
  outerDomain: outerDomainInput,
  selection,
  mode,
  minimumDurationMs = 1,
  formatValue,
  onManualStart,
  onCommit,
  testId = 'chart-range-selector',
}: ChartRangeSelectorProps) {
  const statusId = useId();
  const trackRef = useRef<HTMLDivElement>(null);
  const interactionRef = useRef<PointerInteraction | null>(null);
  const draftRef = useRef<TimestampRange | null>(null);
  const [draftSelection, setDraftSelection] = useState<TimestampRange | null>(null);
  const [interactionDomain, setInteractionDomain] = useState<TimestampRange | null>(null);
  const outerDomain = clampTimestampRange(outerDomainInput, outerDomainInput);
  const domainDuration = Math.max(0, outerDomain.endMs - outerDomain.startMs);
  const minimumDuration = Math.min(
    domainDuration,
    Math.max(0, Number.isFinite(minimumDurationMs) ? minimumDurationMs : 1),
  );
  const committedSelection = clampTimestampRange(selection, outerDomain, minimumDuration);
  const displayedDomain = draftSelection && interactionDomain
    ? interactionDomain
    : outerDomain;
  const displayedSelection = clampTimestampRange(
    draftSelection ?? committedSelection,
    displayedDomain,
    minimumDuration,
  );
  const startPercent = positionPercent(displayedSelection.startMs, displayedDomain);
  const endPercent = positionPercent(displayedSelection.endMs, displayedDomain);
  const keyboardStep = Math.max(1, Math.round(domainDuration / 100));
  const keyboardPageStep = Math.max(keyboardStep, Math.round(domainDuration / 10));
  const statusText = `${label}: ${formatValue(committedSelection.startMs)} to ${formatValue(committedSelection.endMs)}`;

  const updateDraft = (nextRange: TimestampRange) => {
    const interaction = interactionRef.current;
    const next = clampTimestampRange(
      nextRange,
      interaction?.outerDomain ?? outerDomain,
      minimumDuration,
    );
    if (interaction
      && !interaction.manualStarted
      && !timestampRangesEqual(next, interaction.initialRange)) {
      interaction.manualStarted = true;
      if (mode !== 'manual') onManualStart(interaction.initialRange);
    }
    draftRef.current = next;
    setDraftSelection(next);
  };

  const beginPointerInteraction = (part: RangePart, event: PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0 || domainDuration <= 0) return;
    event.preventDefault();
    event.stopPropagation();
    const current = displayedSelection;
    interactionRef.current = {
      pointerId: event.pointerId,
      part,
      manualStarted: false,
      initialClientX: event.clientX,
      initialRange: current,
      outerDomain,
      trackWidth: trackRef.current?.getBoundingClientRect().width ?? 0,
    };
    draftRef.current = current;
    setInteractionDomain(outerDomain);
    setDraftSelection(current);
    pointerCapture(event.currentTarget, event.pointerId);
  };

  const movePointer = (event: PointerEvent<HTMLButtonElement>) => {
    const interaction = interactionRef.current;
    if (!interaction || interaction.pointerId !== event.pointerId) return;
    if (interaction.trackWidth <= 0) return;
    event.preventDefault();
    const interactionDuration = interaction.outerDomain.endMs - interaction.outerDomain.startMs;
    const deltaMs = Math.round(((event.clientX - interaction.initialClientX) / interaction.trackWidth) * interactionDuration);
    if (interaction.part === 'window') {
      updateDraft(moveTimestampRange(interaction.initialRange, deltaMs, interaction.outerDomain));
      return;
    }
    const edge = interaction.part === 'start'
      ? interaction.initialRange.startMs
      : interaction.initialRange.endMs;
    updateDraft(resizeRange(
      interaction.initialRange,
      interaction.part,
      edge + deltaMs,
      interaction.outerDomain,
      minimumDuration,
    ));
  };

  const finishPointer = (event: PointerEvent<HTMLButtonElement>, commit: boolean) => {
    const interaction = interactionRef.current;
    if (!interaction || interaction.pointerId !== event.pointerId) return;
    interactionRef.current = null;
    releasePointerCapture(event.currentTarget, event.pointerId);
    const finalRange = clampTimestampRange(
      draftRef.current ?? interaction.initialRange,
      interaction.outerDomain,
      minimumDuration,
    );
    draftRef.current = null;
    setInteractionDomain(null);
    setDraftSelection(null);
    if (commit && !timestampRangesEqual(finalRange, interaction.initialRange)) {
      if (!interaction.manualStarted && mode !== 'manual') onManualStart(interaction.initialRange);
      onCommit(finalRange);
    }
  };

  const handleKeyboard = (part: RangePart) => (event: KeyboardEvent<HTMLButtonElement>) => {
    let deltaMs: number | null = null;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowDown') deltaMs = -keyboardStep;
    if (event.key === 'ArrowRight' || event.key === 'ArrowUp') deltaMs = keyboardStep;
    if (event.key === 'PageDown') deltaMs = -keyboardPageStep;
    if (event.key === 'PageUp') deltaMs = keyboardPageStep;

    let next: TimestampRange | null = null;
    if (part === 'window') {
      if (deltaMs !== null) next = moveTimestampRange(committedSelection, deltaMs, outerDomain);
      if (event.key === 'Home') {
        next = moveTimestampRange(
          committedSelection,
          outerDomain.startMs - committedSelection.startMs,
          outerDomain,
        );
      }
      if (event.key === 'End') {
        next = moveTimestampRange(
          committedSelection,
          outerDomain.endMs - committedSelection.endMs,
          outerDomain,
        );
      }
    } else {
      const currentValue = part === 'start' ? committedSelection.startMs : committedSelection.endMs;
      if (deltaMs !== null) {
        next = resizeRange(
          committedSelection,
          part,
          currentValue + deltaMs,
          outerDomain,
          minimumDuration,
        );
      }
      if (event.key === 'Home') {
        next = resizeRange(
          committedSelection,
          part,
          part === 'start' ? outerDomain.startMs : committedSelection.startMs + minimumDuration,
          outerDomain,
          minimumDuration,
        );
      }
      if (event.key === 'End') {
        next = resizeRange(
          committedSelection,
          part,
          part === 'start' ? committedSelection.endMs - minimumDuration : outerDomain.endMs,
          outerDomain,
          minimumDuration,
        );
      }
    }

    if (!next || timestampRangesEqual(next, committedSelection)) return;
    event.preventDefault();
    event.stopPropagation();
    if (mode !== 'manual') onManualStart(committedSelection);
    onCommit(next);
  };

  const commonPointerHandlers = {
    onPointerMove: movePointer,
    onPointerUp: (event: PointerEvent<HTMLButtonElement>) => finishPointer(event, true),
    onPointerCancel: (event: PointerEvent<HTMLButtonElement>) => finishPointer(event, false),
    onLostPointerCapture: (event: PointerEvent<HTMLButtonElement>) => finishPointer(event, false),
  };
  const selectionStyle = {
    '--range-start': `${startPercent}%`,
    '--range-end': `${endPercent}%`,
    '--range-center': `${(startPercent + endPercent) / 2}%`,
    '--range-width': `${Math.max(0, endPercent - startPercent)}%`,
  } as CSSProperties;
  const windowDuration = displayedSelection.endMs - displayedSelection.startMs;
  const windowHalfDuration = Math.round(windowDuration / 2);

  return <div
    className="chart-range-selector"
    data-mode={mode}
    data-selection-start={displayedSelection.startMs}
    data-selection-end={displayedSelection.endMs}
    style={selectionStyle}
  >
    <div className="chart-range-selector__heading">
      <span>{label}</span>
      <span>{mode === 'manual' ? 'Manual range' : 'Following full range'}</span>
    </div>
    <div
      ref={trackRef}
      className="chart-range-selector__track"
      data-testid={`${testId}-track`}
      aria-label={label}
    >
      <span className="chart-range-selector__rail" aria-hidden="true" />
      <span className="chart-range-selector__fill" aria-hidden="true" />
      <button
        type="button"
        role="slider"
        className="chart-range-selector__window"
        data-testid={`${testId}-window`}
        aria-label={`${label} selected window`}
        aria-valuemin={displayedDomain.startMs + windowHalfDuration}
        aria-valuemax={displayedDomain.endMs - windowHalfDuration}
        aria-valuenow={Math.round((displayedSelection.startMs + displayedSelection.endMs) / 2)}
        aria-valuetext={`${formatValue(displayedSelection.startMs)} to ${formatValue(displayedSelection.endMs)}`}
        aria-describedby={statusId}
        onPointerDown={(event) => beginPointerInteraction('window', event)}
        onKeyDown={handleKeyboard('window')}
        {...commonPointerHandlers}
      />
      <button
        type="button"
        role="slider"
        className="chart-range-selector__thumb chart-range-selector__thumb--start"
        data-testid={`${testId}-start`}
        aria-label={`${label} start`}
        aria-valuemin={displayedDomain.startMs}
        aria-valuemax={displayedSelection.endMs - minimumDuration}
        aria-valuenow={displayedSelection.startMs}
        aria-valuetext={formatValue(displayedSelection.startMs)}
        aria-describedby={statusId}
        onPointerDown={(event) => beginPointerInteraction('start', event)}
        onKeyDown={handleKeyboard('start')}
        {...commonPointerHandlers}
      ><span aria-hidden="true" /></button>
      <button
        type="button"
        role="slider"
        className="chart-range-selector__thumb chart-range-selector__thumb--end"
        data-testid={`${testId}-end`}
        aria-label={`${label} end`}
        aria-valuemin={displayedSelection.startMs + minimumDuration}
        aria-valuemax={displayedDomain.endMs}
        aria-valuenow={displayedSelection.endMs}
        aria-valuetext={formatValue(displayedSelection.endMs)}
        aria-describedby={statusId}
        onPointerDown={(event) => beginPointerInteraction('end', event)}
        onKeyDown={handleKeyboard('end')}
        {...commonPointerHandlers}
      ><span aria-hidden="true" /></button>
    </div>
    <div className="chart-range-selector__values" aria-hidden="true">
      <span>{formatValue(displayedSelection.startMs)}</span>
      <span>{formatValue(displayedSelection.endMs)}</span>
    </div>
    <p
      id={statusId}
      className="chart-range-selector__status"
      data-testid={`${testId}-status`}
      aria-live="polite"
      aria-atomic="true"
    >{statusText}</p>
  </div>;
}
