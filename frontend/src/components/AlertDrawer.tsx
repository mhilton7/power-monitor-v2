import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bell, Check, Clock3 } from 'lucide-react';
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
  const query = useQuery({ queryKey: ['alerts'], queryFn: api.alerts, refetchInterval: 30_000, enabled: open });
  const acknowledge = useMutation({ mutationFn: api.acknowledgeAlert, onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['alerts'] }) });
  const silence = useMutation({ mutationFn: (alert: Alert) => api.silenceAlert(alert.id, new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString()), onSuccess: () => { setSilenceTarget(null); void queryClient.invalidateQueries({ queryKey: ['alerts'] }); } });
  const actionError = acknowledge.error ?? silence.error;

  return <Dialog open={open} title="Alerts & notifications" description="Evidence-backed alerts are debounced to avoid transient notification floods." onClose={onClose} wide>
    {query.isLoading && <Loading label="Loading alerts" />}
    {query.isError && <ErrorState error={query.error} retry={() => void query.refetch()} />}
    {(acknowledge.isError || silence.isError) && <Notice kind="warning">{actionError instanceof Error ? actionError.message : 'The alert action failed.'}</Notice>}
    {query.data?.alerts.length === 0 && <EmptyState title="No alerts" detail="All monitored systems are within their configured thresholds." />}
    <div className="alert-list">
      {query.data?.alerts.map((alert) => <article className="alert-row" key={alert.id}>
        <div className={`alert-icon alert-${alert.severity}`}><Bell aria-hidden="true" /></div>
        <div className="alert-content"><div className="alert-title-line"><h3>{alert.type.replaceAll('_', ' ')}</h3><StatusPill state={alert.state} /></div>{Object.keys(alert.evidence).length ? <dl className="alert-evidence">{Object.entries(alert.evidence).map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' ? String(value) : 'Structured evidence recorded'}</dd></div>)}</dl> : <p>No additional evidence was returned.</p>}<small>Opened {dateTime(alert.opened_at)}</small></div>
        <div className="alert-actions">
          {alert.state === 'open' && <button type="button" className="icon-button" aria-label={`Acknowledge ${alert.type.replaceAll('_', ' ')}`} onClick={() => acknowledge.mutate(alert.id)} disabled={acknowledge.isPending}><Check aria-hidden="true" /></button>}
          {can('system.manage') && <button type="button" className="icon-button" aria-label={`Silence ${alert.type.replaceAll('_', ' ')} for 24 hours`} onClick={() => setSilenceTarget(alert)}><Clock3 aria-hidden="true" /></button>}
        </div>
      </article>)}
    </div>
    <ConfirmDialog open={silenceTarget !== null} title="Silence this alert for 24 hours?" description={<p>The underlying evidence and alert remain recorded. New notifications for this alert are suppressed until the server-side expiry.</p>} confirmLabel="Silence 24 hours" busy={silence.isPending} onCancel={() => setSilenceTarget(null)} onConfirm={() => { if (silenceTarget) silence.mutate(silenceTarget); }} />
  </Dialog>;
}
