import { act, cleanup, render, screen } from '@testing-library/react';
import { HeartbeatAge } from '../src/components/HeartbeatAge';
import { formatHeartbeatAge } from '../src/lib/heartbeatAge';

const BASE_TIME = new Date('2026-08-16T18:00:00.000Z');

describe('browser heartbeat age ticker', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(BASE_TIME);
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('advances from absolute browser time every second', () => {
    render(<HeartbeatAge timestamp="2026-08-16T17:59:55.000Z" />);
    expect(screen.getByText('5s ago')).toBeInTheDocument();
    void act(() => vi.advanceTimersByTime(1_000));
    expect(screen.getByText('6s ago')).toBeInTheDocument();
    void act(() => vi.advanceTimersByTime(9_000));
    expect(screen.getByText('15s ago')).toBeInTheDocument();
  });

  it('formats minute and hour boundaries with stable fields', () => {
    expect(formatHeartbeatAge('2026-08-16T17:59:01.000Z', BASE_TIME.getTime())).toBe('59s ago');
    expect(formatHeartbeatAge('2026-08-16T17:58:59.000Z', BASE_TIME.getTime())).toBe('1m 01s ago');
    expect(formatHeartbeatAge('2026-08-16T16:56:33.000Z', BASE_TIME.getTime())).toBe('1h 03m 27s ago');
    render(<HeartbeatAge timestamp="2026-08-16T17:59:01.000Z" />);
    void act(() => vi.advanceTimersByTime(1_000));
    expect(screen.getByText('1m 00s ago')).toBeInTheDocument();
  });

  it('uses a newly received absolute timestamp immediately', () => {
    const view = render(<HeartbeatAge timestamp="2026-08-16T17:59:30.000Z" />);
    expect(screen.getByText('30s ago')).toBeInTheDocument();
    view.rerender(<HeartbeatAge timestamp="2026-08-16T17:59:58.000Z" />);
    expect(screen.getByText('2s ago')).toBeInTheDocument();
  });

  it('rejects invalid local timestamps and clamps small future skew', () => {
    const view = render(<HeartbeatAge timestamp="not-a-timestamp" />);
    expect(screen.getByText('Not available')).toBeInTheDocument();
    view.rerender(<HeartbeatAge timestamp="2026-08-16T18:00:05.000Z" />);
    expect(screen.getByText('0s ago')).toBeInTheDocument();
    view.rerender(<HeartbeatAge timestamp="2026-08-16T19:00:00.000Z" />);
    expect(screen.getByText('Not available')).toBeInTheDocument();
  });

  it('recalculates immediately when a background tab becomes visible', () => {
    const originalVisibility = Object.getOwnPropertyDescriptor(document, 'visibilityState');
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
    try {
      render(<HeartbeatAge timestamp="2026-08-16T17:59:55.000Z" />);
      expect(screen.getByText('5s ago')).toBeInTheDocument();
      vi.setSystemTime(new Date('2026-08-16T18:00:30.000Z'));
      Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
      void act(() => document.dispatchEvent(new Event('visibilitychange')));
      expect(screen.getByText('35s ago')).toBeInTheDocument();
    } finally {
      if (originalVisibility) Object.defineProperty(document, 'visibilityState', originalVisibility);
    }
  });

  it('shares one timer, performs no fetch, and cleans up after the last subscriber', () => {
    const intervalSpy = vi.spyOn(window, 'setInterval');
    const clearSpy = vi.spyOn(window, 'clearInterval');
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const view = render(<><HeartbeatAge timestamp="2026-08-16T17:59:55.000Z" /><HeartbeatAge timestamp="2026-08-16T17:59:50.000Z" /></>);
    expect(intervalSpy.mock.calls.filter((call) => call[1] === 1_000)).toHaveLength(1);
    void act(() => vi.advanceTimersByTime(3_000));
    expect(fetchSpy).not.toHaveBeenCalled();
    view.unmount();
    expect(clearSpy).toHaveBeenCalledTimes(1);
  });

  it('keeps second ticks inside the elapsed-time leaf instead of rerendering its parent', () => {
    let parentRenders = 0;
    function Parent() {
      parentRenders += 1;
      return <div><HeartbeatAge timestamp="2026-08-16T17:59:55.000Z" /></div>;
    }
    render(<Parent />);
    expect(parentRenders).toBe(1);
    void act(() => vi.advanceTimersByTime(2_000));
    expect(screen.getByText('7s ago')).toBeInTheDocument();
    expect(parentRenders).toBe(1);
  });

  it('keeps the exact UTC timestamp accessible without a live announcement', () => {
    render(<HeartbeatAge timestamp="2026-08-16T17:59:55Z" />);
    const value = screen.getByText('5s ago');
    expect(value).toHaveAttribute('datetime', '2026-08-16T17:59:55.000Z');
    expect(value).toHaveAttribute('title', 'Exact heartbeat: 2026-08-16T17:59:55.000Z');
    expect(value).toHaveAttribute('aria-label', '5s ago. Exact heartbeat 2026-08-16T17:59:55.000Z');
    expect(value).toHaveAttribute('aria-live', 'off');
    expect(value).not.toHaveAttribute('role', 'alert');
  });
});
