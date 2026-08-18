import '@testing-library/jest-dom/vitest';

class ResizeObserverMock implements ResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

class EventSourceMock extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSED = 2;
  readonly url: string;
  readonly withCredentials = true;
  readonly readyState = 1;
  onerror: ((ev: Event) => unknown) | null = null;
  onmessage: ((ev: MessageEvent) => unknown) | null = null;
  onopen: ((ev: Event) => unknown) | null = null;
  constructor(url: string | URL) { super(); this.url = String(url); }
  close(): void {}
}

Object.defineProperty(globalThis, 'ResizeObserver', { value: ResizeObserverMock, writable: true });
Object.defineProperty(globalThis, 'EventSource', { value: EventSourceMock, writable: true, configurable: true });
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }),
});
Object.defineProperty(URL, 'createObjectURL', { value: () => 'blob:test', writable: true });
Object.defineProperty(URL, 'revokeObjectURL', { value: () => undefined, writable: true });
