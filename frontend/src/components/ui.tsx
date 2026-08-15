import { AlertTriangle, CheckCircle2, Info, LoaderCircle, X } from 'lucide-react';
import { useEffect, useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { ApiError } from '../api/client';

const dialogStack: HTMLElement[] = [];
let rootAriaHidden: string | null = null;
let rootWasInert = false;
let bodyOverflow = '';
let bodyPaddingRight = '';

function setInert(element: HTMLElement, inert: boolean): void {
  if (inert) element.setAttribute('inert', '');
  else element.removeAttribute('inert');
}

function syncDialogLayers(): void {
  const top = dialogStack.at(-1);
  const root = document.getElementById('root');
  if (top) {
    if (root) {
      setInert(root, true);
      root.setAttribute('aria-hidden', 'true');
    }
    const scrollbarWidth = document.documentElement.clientWidth > 0 ? Math.max(0, window.innerWidth - document.documentElement.clientWidth) : 0;
    document.body.style.overflow = 'hidden';
    if (scrollbarWidth > 0) document.body.style.paddingRight = `${scrollbarWidth}px`;
  } else {
    if (root) {
      setInert(root, rootWasInert);
      if (rootAriaHidden === null) root.removeAttribute('aria-hidden');
      else root.setAttribute('aria-hidden', rootAriaHidden);
    }
    document.body.style.overflow = bodyOverflow;
    document.body.style.paddingRight = bodyPaddingRight;
  }
  for (const layer of dialogStack) {
    const inactive = layer !== top;
    setInert(layer, inactive);
    if (inactive) layer.setAttribute('aria-hidden', 'true');
    else layer.removeAttribute('aria-hidden');
  }
}

export function Card({ title, eyebrow, action, children, className = '' }: {
  title?: string;
  eyebrow?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return <section className={`card ${className}`}>
    {(title || eyebrow || action) && <header className="card-header">
      <div>{eyebrow && <p className="eyebrow">{eyebrow}</p>}{title && <h2>{title}</h2>}</div>
      {action}
    </header>}
    {children}
  </section>;
}

export function StatusPill({ state, label }: { state: string; label?: string }) {
  const normalized = ['live', 'healthy', 'current', 'succeeded', 'complete', 'online'].includes(state)
    ? 'ok'
    : ['offline', 'critical', 'failed', 'unhealthy', 'invalid'].includes(state)
      ? 'danger'
      : ['waiting', 'stale', 'warning', 'degraded', 'needs_attention', 'review_required', 'running'].includes(state)
        ? 'warn'
        : 'neutral';
  return <span className={`status-pill status-${normalized}`}><span aria-hidden="true" />{label ?? state.replaceAll('_', ' ')}</span>;
}

export function Loading({ label = 'Loading' }: { label?: string }) {
  return <div className="loading" role="status"><LoaderCircle className="spin" aria-hidden="true" /> <span>{label}</span></div>;
}

export function EmptyState({ title, detail }: { title: string; detail: string }) {
  return <div className="empty-state"><Info aria-hidden="true" /><h2>{title}</h2><p>{detail}</p></div>;
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const forbidden = error instanceof ApiError && error.status === 403;
  return <div className="error-state" role="alert">
    <AlertTriangle aria-hidden="true" />
    <div><h2>{forbidden ? 'Permission required' : 'Unable to load this view'}</h2><p>{error instanceof Error ? error.message : 'An unexpected error occurred.'}</p></div>
    {retry && !forbidden && <button type="button" className="button button-secondary" onClick={retry}>Try again</button>}
  </div>;
}

export function Notice({ kind = 'info', children }: { kind?: 'info' | 'warning' | 'success'; children: ReactNode }) {
  const Icon = kind === 'warning' ? AlertTriangle : kind === 'success' ? CheckCircle2 : Info;
  return <div className={`notice notice-${kind}`} role={kind === 'warning' ? 'alert' : 'status'}><Icon aria-hidden="true" /><div>{children}</div></div>;
}

interface DialogProps {
  open: boolean;
  title: string;
  description?: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}

export function Dialog({ open, title, description, onClose, children, wide = false }: DialogProps) {
  const titleId = useId();
  const descriptionId = useId();
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const backdropRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    const prior = document.activeElement as HTMLElement | null;
    const backdrop = backdropRef.current;
    if (!backdrop) return;
    if (dialogStack.length === 0) {
      const root = document.getElementById('root');
      rootAriaHidden = root?.getAttribute('aria-hidden') ?? null;
      rootWasInert = root?.hasAttribute('inert') ?? false;
      bodyOverflow = document.body.style.overflow;
      bodyPaddingRight = document.body.style.paddingRight;
    }
    dialogStack.push(backdrop);
    syncDialogLayers();
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (dialogStack.at(-1) !== backdrop) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [contenteditable="true"], [tabindex]:not([tabindex="-1"])',
      )).filter((element) => element.tabIndex >= 0 && element.getAttribute('aria-hidden') !== 'true');
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) {
        event.preventDefault();
        dialog.focus();
      } else if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      const index = dialogStack.lastIndexOf(backdrop);
      if (index >= 0) dialogStack.splice(index, 1);
      syncDialogLayers();
      prior?.focus();
    };
  }, [open]);

  if (!open) return null;
  return createPortal(<div ref={backdropRef} className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target && dialogStack.at(-1) === event.currentTarget) onClose(); }}>
    <section ref={dialogRef} className={`dialog ${wide ? 'dialog-wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={description ? descriptionId : undefined} tabIndex={-1}>
      <header className="dialog-header"><div><h2 id={titleId}>{title}</h2>{description && <p id={descriptionId}>{description}</p>}</div><button ref={closeRef} type="button" className="icon-button" onClick={onClose} aria-label={`Close ${title}`}><X aria-hidden="true" /></button></header>
      <div className="dialog-body">{children}</div>
    </section>
  </div>, document.body);
}

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: ReactNode;
  confirmLabel: string;
  onCancel: () => void;
  onConfirm: () => void;
  typedPhrase?: string;
  busy?: boolean;
  confirmDisabled?: boolean;
  tone?: 'danger' | 'warning';
}

export function ConfirmDialog({ open, title, description, confirmLabel, onCancel, onConfirm, typedPhrase, busy = false, confirmDisabled = false, tone = 'danger' }: ConfirmDialogProps) {
  const inputId = useId();
  const valueRef = useRef<HTMLInputElement>(null);
  return <Dialog open={open} title={title} onClose={onCancel}>
    <div className={`confirm-message confirm-${tone}`}><AlertTriangle aria-hidden="true" /><div>{description}</div></div>
    {typedPhrase && <div className="field"><label htmlFor={inputId}>Type <strong>{typedPhrase}</strong> to continue</label><input ref={valueRef} id={inputId} autoComplete="off" spellCheck={false} onInput={(event) => event.currentTarget.setCustomValidity('')} /></div>}
    <div className="dialog-actions"><button type="button" className="button button-secondary" onClick={onCancel} disabled={busy}>Cancel</button><button type="button" className="button button-danger" disabled={busy || confirmDisabled} onClick={() => {
      if (typedPhrase && valueRef.current?.value !== typedPhrase) {
        valueRef.current?.setCustomValidity(`Type ${typedPhrase} exactly.`);
        valueRef.current?.reportValidity();
        return;
      }
      onConfirm();
    }}>{busy ? 'Working…' : confirmLabel}</button></div>
  </Dialog>;
}
