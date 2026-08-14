import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, CalendarClock, FileCheck2, FileLock2, RefreshCw, ShieldCheck, Upload } from 'lucide-react';
import { useRef, useState, type FormEvent } from 'react';
import { api } from '../api';
import type { RateDraft } from '../api/schemas';
import { PermissionGate } from '../auth/PermissionGate';
import { Card, ConfirmDialog, Dialog, EmptyState, ErrorState, Loading, Notice, StatusPill } from '../components/ui';
import { dateTime, money, numeric, percent } from '../lib/format';
import { formString } from '../lib/form';

const correctionFields = [
  { key: 'rate_plan_name', label: 'Rate plan name' },
  { key: 'rate_class', label: 'Rate class' },
  { key: 'cca_or_direct_access_indicator', label: 'Generation service indicator' },
  { key: 'baseline_allocation_rule', label: 'Baseline allocation rule' },
  { key: 'baseline_credit_rate', label: 'Baseline credit rate' },
] as const;

function draftValue(draft: RateDraft, key: (typeof correctionFields)[number]['key']): string {
  const value = draft[key];
  return value === null ? '' : String(value);
}

export function BillingPage() {
  const queryClient = useQueryClient();
  const formRef = useRef<HTMLFormElement>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [review, setReview] = useState<RateDraft | null>(null);
  const [publishAt, setPublishAt] = useState('');
  const [publishOpen, setPublishOpen] = useState(false);
  const billing = useQuery({ queryKey: ['billing'], queryFn: api.billing, refetchInterval: 60_000 });
  const home = useQuery({ queryKey: ['home'], queryFn: api.home, refetchInterval: 60_000 });
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 60_000 });
  const checkRates = useMutation({ mutationFn: api.checkRates, onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['health'] }) });
  const upload = useMutation({ mutationFn: api.uploadRatePdf, onSuccess: (draft) => { formRef.current?.reset(); setReview(draft); void queryClient.invalidateQueries({ queryKey: ['billing'] }); } });
  const correct = useMutation({
    mutationFn: async ({ draft, form }: { draft: RateDraft; form: FormData }) => {
      let current = draft;
      for (const field of correctionFields) {
        const next = formString(form, field.key);
        if (next && next !== draftValue(current, field.key)) current = await api.correctRateDraft(current.id, field.key, next);
      }
      return current;
    },
    onSuccess: (draft) => { setReview(draft); void queryClient.invalidateQueries({ queryKey: ['billing'] }); },
  });
  const publish = useMutation({
    mutationFn: () => api.publishRateDraft(review?.id ?? '', new Date(publishAt).toISOString(), billing.data?.accounts[0]?.utility_account_id),
    onSuccess: () => { setPublishOpen(false); setReview(null); setImportOpen(false); void queryClient.invalidateQueries({ queryKey: ['billing'] }); },
  });

  function closeImport() { formRef.current?.reset(); setReview(null); setPublishAt(''); setImportOpen(false); }
  function submitPdf(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const input = event.currentTarget.elements.namedItem('rateDocument');
    if (!(input instanceof HTMLInputElement) || !input.files?.[0]) return;
    const file = input.files[0];
    if (file.type !== 'application/pdf' || !file.name.toLowerCase().endsWith('.pdf')) { input.setCustomValidity('Select a PDF document.'); input.reportValidity(); return; }
    if (file.size > 15 * 1024 * 1024) { input.setCustomValidity('The document must be 15 MiB or smaller.'); input.reportValidity(); return; }
    input.setCustomValidity(''); upload.mutate(file);
  }
  function submitCorrections(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (review) correct.mutate({ draft: review, form: new FormData(event.currentTarget) }); }

  if (billing.isLoading) return <Loading label="Loading published rates and sensor-derived estimates" />;
  if (billing.isError) return <ErrorState error={billing.error} retry={() => void billing.refetch()} />;
  const data = billing.data;
  if (!data) return <ErrorState error={new Error('The billing response was empty.')} retry={() => void billing.refetch()} />;
  const account = data.accounts[0];
  const currentRate = home.data?.current_rate;
  const summary = home.data?.summaries;
  const summaryScopeId = home.data?.summary_scope?.aggregate ? home.data.summary_scope.circuit_id : home.data?.summary_scope?.device_id;
  const selectedEstimate = account?.estimates.find((estimate) => estimate.scope_id === summaryScopeId);

  return <div className="page billing-page">
    <header className="page-heading"><div><p className="eyebrow">Reusable rates applied to sensor intervals</p><h1>Billing</h1><p>Review rate plans and estimates without importing customer billing history.</p></div><PermissionGate permission="rates.bill_import"><button type="button" className="button button-primary" onClick={() => setImportOpen(true)}><Upload aria-hidden="true" /> Import rates from SCE bill PDF</button></PermissionGate></header>
    <Notice><strong>Rate-source boundary:</strong> a PDF can create only a reviewable reusable-rate draft. Electrical usage, History, completeness and energy calculations come exclusively from authenticated PZEM sensor intervals. Uploading never activates a rate.</Notice>
    <section className="billing-overview">
      <Card title="Current plan" eyebrow="Immutable published assignment" className="current-plan-card">
        {account?.plan_name ? <><div className="plan-title"><div><strong>{account.plan_name}</strong><span>{account.rate_version_id ?? 'Version unavailable'}</span></div><StatusPill state="current" /></div><dl><div><dt>Effective</dt><dd>{dateTime(account.effective_start)}</dd></div><div><dt>Monitored scope</dt><dd>{account.cost_scope.replaceAll('_', ' ')}</dd></div><div><dt>Fixed charges</dt><dd>{account.fixed_charges_included ? 'Included' : 'Excluded'}</dd></div><div><dt>Baseline credit</dt><dd>{account.baseline_credit_included ? 'Included' : 'Excluded'}</dd></div><div><dt>CCA / Direct Access</dt><dd>{account.cca_or_direct_access ?? 'Not configured'}</dd></div></dl></> : <EmptyState title="No current plan" detail="Publish and assign a reviewed effective-dated rate version to calculate costs." />}
      </Card>
      <Card title="Current period" eyebrow="Server-evaluated schedule" className="period-card"><CalendarClock aria-hidden="true" /><strong>{currentRate?.period ?? 'Not available'}</strong><span>{currentRate?.next_change_at ? `Next schedule boundary ${dateTime(currentRate.next_change_at)}` : 'Next schedule boundary unavailable'}</span><div><small>Billing-cycle progress</small><b>{numeric(summary?.billing_cycle.energy_kwh === null || summary?.billing_cycle.energy_kwh === undefined ? null : Number(summary.billing_cycle.energy_kwh), 'kWh')}</b><span>{money(summary?.billing_cycle.cost)} estimated · {percent(summary?.billing_cycle.completeness === null || summary?.billing_cycle.completeness === undefined ? null : Number(summary.billing_cycle.completeness))} complete</span></div></Card>
      <Card title="Official SCE source" eyebrow="Allowlisted server-side synchronization" className="source-card"><div className="source-state"><ShieldCheck aria-hidden="true" /><div><strong>Southern California Edison</strong><StatusPill state={health.data?.last_rate_sync?.state ?? 'never_checked'} /></div></div><p>Last run {dateTime(health.data?.last_rate_sync?.completed_at)}</p><PermissionGate permission="rates.sync"><button type="button" className="button button-secondary" onClick={() => checkRates.mutate()} disabled={checkRates.isPending}><RefreshCw className={checkRates.isPending ? 'spin' : ''} aria-hidden="true" /> {checkRates.isPending ? 'Checking…' : 'Check now'}</button></PermissionGate>{checkRates.isSuccess && <small role="status">Sync run {checkRates.data.run_id} queued for validation and review.</small>}</Card>
    </section>
    <Card title="Sensor-derived estimates" eyebrow="Published rate assignment applied to authenticated committed intervals">
      <div className="estimate-grid"><Estimate label="Today" summary={summary?.today} /><Estimate label="Yesterday" /><Estimate label="This week" summary={summary?.week} /><Estimate label="Last week" /><Estimate label="This month" /><Estimate label="Billing cycle to date" summary={summary?.billing_cycle} /><Estimate label="Projected billing cycle" /></div>
      {selectedEstimate && <dl className="estimate-breakdown"><div><dt>Authenticated sensor energy</dt><dd>{numeric(Number(selectedEstimate.sensor_energy_kwh), 'kWh')}</dd></div><div><dt>Usage-based cost</dt><dd>{money(selectedEstimate.energy_cost)}</dd></div><div><dt>Configured fixed charges</dt><dd>{money(selectedEstimate.fixed_charge)}</dd></div><div><dt>Configured credits</dt><dd>-{money(selectedEstimate.credits)}</dd></div><div><dt>Selected immutable rate</dt><dd>{selectedEstimate.rate_plan_version_id}</dd></div><div><dt>Missing sensor intervals</dt><dd>{selectedEstimate.missing_intervals}</dd></div></dl>}
      <p className="disclosure">Unavailable scopes remain unavailable; no value is inferred from a bill. Estimates can differ from a utility bill because of meter accuracy, unmonitored loads, rate changes, taxes, credits, rounding and utility adjustments. A one-CT sensor defaults to energy-only scope.</p>
    </Card>
    <Card title="Rate-plan drafts" eyebrow="Review, correct, publish, then explicitly assign">
      {data.drafts.length === 0 ? <EmptyState title="No drafts awaiting review" detail="Official sync candidates and permitted PDF rate extractions appear here." /> : <div className="draft-list">{data.drafts.map((draft) => <button type="button" key={draft.id} onClick={() => { setReview(draft); setImportOpen(true); }}><FileCheck2 aria-hidden="true" /><div><strong>{draft.rate_plan_name ?? 'Unnamed rate draft'}</strong><span>{draft.utility_name ?? 'Utility not identified'} · {draft.source_evidence.length} allowed evidence fields</span></div><StatusPill state={draft.state} /><ArrowRight aria-hidden="true" /></button>)}</div>}
    </Card>
    <Dialog open={importOpen} title="Import rates from SCE bill PDF" description="Local server-side extraction produces only a closed, allowlisted RatePlanDraft." onClose={closeImport} wide>
      {!review ? <><div className="boundary-panel"><FileLock2 aria-hidden="true" /><div><h3>Rates and reusable cost rules only</h3><p>The document is used only to identify reusable schedule and pricing fields. Customer identity, service details, meter readings, consumption, balances, payments, amount due, bill totals and historical charts are discarded and never shown.</p><p>No upload can create or change sensor readings, intervals, History, completeness, calibration, forecasts, energy totals or costs until a separately reviewed rate version is published and assigned.</p></div></div><form ref={formRef} className="upload-form" onSubmit={submitPdf}><div className="file-drop"><Upload aria-hidden="true" /><label htmlFor="rate-document">Choose an SCE PDF rate source</label><input id="rate-document" name="rateDocument" type="file" accept="application/pdf,.pdf" required aria-describedby="upload-limits" /><small id="upload-limits">PDF only · maximum 15 MiB · server-enforced page and processing limits · no cloud OCR</small></div>{upload.isError && <p className="form-error" role="alert">{upload.error instanceof Error ? upload.error.message : 'The document could not be processed.'}</p>}<div className="dialog-actions"><button type="button" className="button button-secondary" onClick={closeImport}>Cancel</button><button type="submit" className="button button-primary" disabled={upload.isPending}>{upload.isPending ? 'Extracting allowed rates…' : 'Create rate draft'}</button></div></form></> : <form className="rate-review" onSubmit={submitCorrections}>
        <Notice kind="warning"><strong>Review required.</strong> Parsing does not activate, publish or assign a rate. Confirm every allowed field directly against the source and an official effective date.</Notice>
        <div className="review-meta"><div><span>Utility</span><strong>{review.utility_name ?? 'Not identified'}</strong></div><div><span>Plan</span><strong>{review.rate_plan_name ?? 'Not identified'}</strong></div><div><span>Parser</span><strong>{review.parser_version}</strong></div><div><span>Source SHA-256</span><code title={review.artifact_sha256}>{review.artifact_sha256.slice(0, 18)}…</code></div></div>
        <div className="rate-field-list">{correctionFields.map((field) => <div className="rate-field" key={field.key}><div className="field"><label htmlFor={`rate-${field.key}`}>{field.label}</label><input id={`rate-${field.key}`} name={field.key} defaultValue={draftValue(review, field.key)} /></div><div className="field-evidence"><span>Allowlisted field</span><span>{review.source_evidence.length} source evidence item{review.source_evidence.length === 1 ? '' : 's'}</span></div></div>)}</div>
        {correct.isError && <p className="form-error" role="alert">{correct.error instanceof Error ? correct.error.message : 'Corrections could not be saved.'}</p>}
        <div className="dialog-actions"><button type="button" className="button button-secondary" onClick={closeImport}>Close review</button><button type="submit" className="button button-secondary" disabled={correct.isPending}>{correct.isPending ? 'Saving…' : 'Save corrections'}</button><PermissionGate permission="rates.manage"><button type="button" className="button button-primary" onClick={() => setPublishOpen(true)}>Publish version</button></PermissionGate></div>
      </form>}
    </Dialog>
    <ConfirmDialog open={publishOpen} title="Publish and assign an immutable rate version?" description={<div><p>Publishing is separate from extraction. This version can affect estimates only from its explicit effective date and only when assigned to sensor-derived intervals.</p><div className="field"><label htmlFor="rate-effective-at">Effective date and time</label><input id="rate-effective-at" type="datetime-local" value={publishAt} onChange={(event) => setPublishAt(event.target.value)} required /></div></div>} confirmLabel="Publish rate version" busy={publish.isPending} onCancel={() => setPublishOpen(false)} onConfirm={() => { if (publishAt) publish.mutate(); }} tone="warning" />
  </div>;
}

function Estimate({ label, summary }: { label: string; summary?: { energy_kwh: string | number | null; cost: string | number | null; completeness: string | number | null } | undefined }) {
  return <article><span>{label}</span><strong>{money(summary?.cost)}</strong><small>{numeric(summary?.energy_kwh === null || summary?.energy_kwh === undefined ? null : Number(summary.energy_kwh), 'kWh')} · {percent(summary?.completeness === null || summary?.completeness === undefined ? null : Number(summary.completeness))} complete</small></article>;
}
