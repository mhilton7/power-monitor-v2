import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bell, Check, Clock3, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { api } from '../api';
import type { Alert } from '../api/schemas';
import { useSession } from '../auth/SessionContext';
import { dateTime } from '../lib/format';
import { ConfirmDialog, Dialog, EmptyState, ErrorState, Loading, Notice, StatusPill } from './ui';

export function AlertDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { can } = useSession();
  const queryClient = useQueryClient();
  const [silenceTarget, setSilenceTarget] = useState<Alert | null>(null);
  const [dismissTarget, setDismissTarget] = useState<Alert | null>(null);
  const [clearAllOpen, setClearAllOpen] = useState(false);
  const query = useQuery({ queryKey: ['alerts'], queryFn: api.alerts, refetchInterval: 30_000, enabled: open });
  const acknowledge = useMutation({ mutationFn: api.acknowledgeAlert, onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['alerts'] }) });
  const silence = useMutation({ mutationFn: (alert: Alert) => api.silenceAlert(alert.id, new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString()), onSuccess: () => { setSilenceTarget(null); void queryClient.invalidateQueries({ queryKey: ['alerts'] }); } });
  const dismiss = useMutation({ mutationFn: api.dismissAlertNotification, onSuccess: () => { setDismissTarget(null); void queryClient.invalidateQueries({ queryKey: ['alerts'] }); } });
  const clearAll = useMutation({ mutationFn: api.dismissAllAlertNotifications, onSuccess: () => { setClearAllOpen(false); void queryClient.invalidateQueries({ queryKey: ['alerts'] }); } });
  const actionError = acknowledge.error ?? silence.error ?? dismiss.error ?? clearAll.error;

  return <Dialog open={open} title="Alerts & notifications" description="Alerts span all homes this account can access. Evidence-backed alerts are debounced to avoid transient notification floods." onClose={onClose} wide>
    {query.isLoading && <Loading label="Loading alerts" />}
    {query.isError && <ErrorState error={query.error} retry={() => void query.refetch()} />}
    {(acknowledge.isError || silence.isError || dismiss.isError || clearAll.isError) && <Notice kind="warning">{actionError instanceof Error ? actionError.message : 'The alert action failed.'}</Notice>}
    {query.data?.alerts.length === 0 && <EmptyState title="No alerts" detail="All monitored systems are within their configured thresholds." />}
    {Boolean(query.data?.alerts.length) && <div className="alert-list-toolbar"><span>{query.data?.alerts.length} notification{query.data?.alerts.length === 1 ? '' : 's'}</span><button type="button" className="button button-secondary" onClick={() => setClearAllOpen(true)}>Clear all</button></div>}
    <div className="alert-list">
      {query.data?.alerts.map((alert) => <article className="alert-row" key={alert.id}>
        <div className={`alert-icon alert-${alert.severity}`}><Bell aria-hidden="true" /></div>
        <div className="alert-content"><div className="alert-title-line"><h3>{alert.type.replaceAll('_', ' ')}</h3><StatusPill state={alert.state} /></div>{Object.keys(alert.evidence).length ? <dl className="alert-evidence">{Object.entries(alert.evidence).map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' ? String(value) : 'Structured evidence recorded'}</dd></div>)}</dl> : <p>No additional evidence was returned.</p>}<small>Opened {dateTime(alert.opened_at)}</small></div>
        <div className="alert-actions">
          {alert.state === 'open' && <button type="button" className="icon-button" aria-label={`Acknowledge ${alert.type.replaceAll('_', ' ')}`} onClick={() => acknowledge.mutate(alert.id)} disabled={acknowledge.isPending}><Check aria-hidden="true" /></button>}
          {can('system.manage') && <button type="button" className="icon-button" aria-label={`Silence ${alert.type.replaceAll('_', ' ')} for 24 hours`} onClick={() => setSilenceTarget(alert)}><Clock3 aria-hidden="true" /></button>}
          <button type="button" className="icon-button" aria-label={`Remove ${alert.type.replaceAll('_', ' ')} notification`} onClick={() => setDismissTarget(alert)} disabled={dismiss.isPending || clearAll.isPending}><Trash2 aria-hidden="true" /></button>
        </div>
      </article>)}
    </div>
    <ConfirmDialog open={silenceTarget !== null} title="Silence this alert for 24 hours?" description={<p>The underlying evidence and alert remain recorded. New notifications for this alert are suppressed until the server-side expiry.</p>} confirmLabel="Silence 24 hours" busy={silence.isPending} onCancel={() => setSilenceTarget(null)} onConfirm={() => { if (silenceTarget) silence.mutate(silenceTarget); }} />
    <ConfirmDialog open={dismissTarget !== null} title="Remove this notification?" description={<p>This removes the notification only from your account. The alert and its evidence remain recorded, and a later occurrence can appear as a new alert.</p>} confirmLabel="Remove notification" busy={dismiss.isPending} onCancel={() => setDismissTarget(null)} onConfirm={() => { if (dismissTarget) dismiss.mutate(dismissTarget.id); }} />
    <ConfirmDialog open={clearAllOpen} title="Clear all notifications?" description={<p>This removes every current alert notification only from your account. Alert evidence and lifecycle history remain recorded.</p>} confirmLabel="Clear all" busy={clearAll.isPending} onCancel={() => setClearAllOpen(false)} onConfirm={() => clearAll.mutate()} />
  </Dialog>;
}
