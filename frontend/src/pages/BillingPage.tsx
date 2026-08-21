import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, FileCheck2, FileLock2, Trash2, Upload } from 'lucide-react';
import { useRef, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api';
import type { RateDraft } from '../api/schemas';
import { PermissionGate } from '../auth/PermissionGate';
import { Card, ConfirmDialog, Dialog, EmptyState, ErrorState, Loading, Notice, StatusPill } from '../components/ui';
import { dateTime, money, numeric, percent } from '../lib/format';
import { formString } from '../lib/form';
import { useHomeScope } from '../home/useHomeScope';
import { RateSourceWorkflow } from '../rates/RateSourceWorkflow';

const MAX_RATE_PDF_BYTES = 10 * 1024 * 1024;

const correctionFields = [
  { key: 'rate_plan_name', label: 'Rate plan name' },
  { key: 'rate_class', label: 'Rate class' },
  { key: 'cca_or_direct_access_indicator', label: 'Generation service indicator' },
  { key: 'baseline_allocation_rule', label: 'Baseline allocation rule' },
  { key: 'baseline_credit_rate', label: 'Baseline credit rate' },
  { key: 'billing_period_days', label: 'Billing days' },
] as const;

function draftValue(draft: RateDraft, key: (typeof correctionFields)[number]['key']): string {
  const value = draft[key];
  return value === null ? '' : String(value);
}

function evidenceValue(draft: RateDraft, label: string): string {
  return draft.source_evidence.find((field) => field.supporting_label === label)?.normalized_value ?? 'Not extracted';
}

function normalizedDecimal(value: string | number): string | null {
  const match = String(value).match(/(?:^|=)(\d+(?:\.\d+)?)/);
  if (!match) return null;
  const [whole, fraction = ''] = match[1]!.split('.');
  return `${whole}.${fraction.replace(/0+$/, '')}`;
}

function activeRateComparison(draft: RateDraft, currentRate: { plan_name: string; price_per_kwh: string | number | null } | null | undefined): string {
  if (!currentRate) return 'No active rate is available for comparison.';
  if (currentRate.plan_name !== draft.rate_plan_name) return 'The extracted plan differs from the active plan.';
  if (currentRate.price_per_kwh === null) return 'The active plan has no current unit rate to compare.';
  const active = normalizedDecimal(currentRate.price_per_kwh);
  const candidates = ['Tier 1 all-in rate', 'Tier 2 all-in rate']
    .map((label) => normalizedDecimal(evidenceValue(draft, label)))
    .filter((value): value is string => value !== null);
  return active !== null && candidates.includes(active)
    ? 'The current active unit rate matches one extracted tier; the complete schedule still requires review.'
    : 'The extracted tier rates differ from the current active unit rate.';
}

function decimalText(value: string | number | null): string {
  if (value === null) return 'Review required';
  const number = Number(value);
  return Number.isFinite(number) ? number.toString() : 'Review required';
}

function exactRate(value: string | number | null | undefined, digits: number, suffix: string): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return 'Waiting for rate details';
  return `$${Number(value).toFixed(digits)}${suffix}`;
}

function tierLabel(value: 'tier_1' | 'tier_2' | 'estimated_tier_1' | 'estimated_tier_2' | 'not_confirmed' | undefined, coverage: string | number | null | undefined): string {
  void coverage;
  if (value === 'not_confirmed' || value === undefined) return 'Tier not confirmed';
  if (value === 'estimated_tier_1') return 'Estimated Tier 1';
  if (value === 'estimated_tier_2') return 'Estimated Tier 2';
  return value === 'tier_2' ? 'Tier 2' : 'Tier 1';
}

function pendingNumeric(value: string | number | null, unit: string): string {
  if (unit === 'kWh' && value !== null && Number(value) === 0) return '0.0 kWh';
  return value === null ? '—' : numeric(Number(value), unit);
}

function pendingMoney(value: string | number | null): string {
  return value === null ? '—' : money(value);
}

export function BillingPage({ mode = 'billing' }: { mode?: 'billing' | 'settings' }) {
  const [billingClock] = useState(() => new Date());
  const queryClient = useQueryClient();
  const formRef = useRef<HTMLFormElement>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [review, setReview] = useState<RateDraft | null>(null);
  const [publishAt, setPublishAt] = useState('');
  const [publishOpen, setPublishOpen] = useState(false);
  const [deleteDraftOpen, setDeleteDraftOpen] = useState(false);
  const homeScope = useHomeScope();
  const { selectedHomeId } = homeScope;
  const billing = useQuery({ queryKey: ['billing', selectedHomeId], queryFn: () => api.billing(selectedHomeId), enabled: Boolean(selectedHomeId), refetchInterval: 60_000 });
  const home = useQuery({ queryKey: ['home', selectedHomeId], queryFn: () => api.home(selectedHomeId), enabled: Boolean(selectedHomeId), refetchInterval: 60_000 });
  const upload = useMutation({ mutationFn: (file: File) => api.uploadRatePdf(file, selectedHomeId), onSuccess: (draft) => { formRef.current?.reset(); setReview(draft); void queryClient.invalidateQueries({ queryKey: ['billing'] }); } });
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
    mutationFn: () => {
      if (!review) throw new Error('Choose a reviewed rate draft.');
      const effectiveStart = new Date(publishAt).toISOString();
      return api.publishRateDraft(review.id, effectiveStart, null, billing.data?.accounts[0]?.utility_account_id);
    },
    onSuccess: () => { setPublishOpen(false); setReview(null); setImportOpen(false); void queryClient.invalidateQueries({ queryKey: ['billing'] }); },
  });
  const removeDraft = useMutation({
    mutationFn: () => {
      if (!review) throw new Error('Choose a PDF rate draft.');
      return api.deleteRateDraft(review.id);
    },
    onSuccess: () => {
      setDeleteDraftOpen(false);
      closeImport();
      void queryClient.invalidateQueries({ queryKey: ['billing'] });
    },
  });

  function closeImport() { formRef.current?.reset(); setReview(null); setPublishAt(''); setImportOpen(false); }
  function submitPdf(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const input = event.currentTarget.elements.namedItem('rateDocument');
    if (!(input instanceof HTMLInputElement) || !input.files?.[0]) return;
    const file = input.files[0];
    if (file.type !== 'application/pdf' || !file.name.toLowerCase().endsWith('.pdf')) { input.setCustomValidity('Select a PDF document.'); input.reportValidity(); return; }
    if (file.size > MAX_RATE_PDF_BYTES) { input.setCustomValidity('The document must be 10 MiB or smaller.'); input.reportValidity(); return; }
    input.setCustomValidity(''); upload.mutate(file);
  }
  function submitCorrections(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (review) correct.mutate({ draft: review, form: new FormData(event.currentTarget) }); }

  if (homeScope.isLoading) return <div className="page"><h1 className="sr-only">Billing</h1><Loading label="Loading authorized homes" /></div>;
  if (homeScope.isError) return <div className="page"><h1 className="sr-only">Billing</h1><ErrorState error={homeScope.error} retry={homeScope.refetch} /></div>;
  if (!selectedHomeId) return <div className="page"><h1 className="sr-only">Billing</h1><EmptyState title={homeScope.homeScopes.length === 0 ? 'No authorized home' : 'Choose an active home'} detail={homeScope.homeScopes.length === 0 ? 'Your account has no authorized home scope. Billing remains unavailable.' : 'Select a home from the Active home control before loading billing data.'} /></div>;
  if (billing.isLoading) return <div className="page"><h1 className="sr-only">Billing</h1><Loading label="Loading published rates and sensor-derived estimates" /></div>;
  if (billing.isError) return <div className="page"><h1 className="sr-only">Billing</h1><ErrorState error={billing.error} retry={() => void billing.refetch()} /></div>;
  const data = billing.data;
  if (!data) return <div className="page"><h1 className="sr-only">Billing</h1><ErrorState error={new Error('The billing response was empty.')} retry={() => void billing.refetch()} /></div>;
  const account = data.accounts[0];
  const currentRate = home.data?.current_rate;
  const summary = home.data?.summaries;
  const summaryScopeId = home.data?.summary_scope?.aggregate ? home.data.summary_scope.circuit_id : home.data?.summary_scope?.device_id;
  const selectedEstimate = account?.estimates.find((estimate) => estimate.scope_id === summaryScopeId);
  const cycle = account?.current_billing_cycle;
  const ratePlan = account?.current_rate_plan;
  const tierBreakdown = cycle?.tier_breakdown;
  const cycleDays = cycle ? Math.max(1, Math.ceil((new Date(cycle.end_utc).getTime() - new Date(cycle.start_utc).getTime()) / 86_400_000)) : null;
  const tierBoundary = tierBreakdown?.tier_2.starts_above_kwh ?? cycle?.tier_1_allowance_kwh ?? (ratePlan?.daily_baseline_allowance_kwh !== null && ratePlan?.daily_baseline_allowance_kwh !== undefined && cycleDays !== null ? Number(ratePlan.daily_baseline_allowance_kwh) * cycleDays : null);
  const tierOneUsage = tierBreakdown?.tier_1.usage_kwh ?? cycle?.tier_1_usage_kwh ?? null;
  const tierOneRate = tierBreakdown?.tier_1.rate_per_kwh ?? cycle?.tier_1_rate_per_kwh ?? ratePlan?.tier_1_price_per_kwh;
  const tierOneCost = tierBreakdown?.tier_1.cost ?? cycle?.tier_1_cost ?? null;
  const tierTwoUsage = tierBreakdown?.tier_2.usage_kwh ?? cycle?.tier_2_usage_kwh ?? null;
  const tierTwoRate = tierBreakdown?.tier_2.rate_per_kwh ?? cycle?.tier_2_rate_per_kwh ?? ratePlan?.tier_2_price_per_kwh;
  const tierTwoCost = tierBreakdown?.tier_2.cost ?? cycle?.tier_2_cost ?? null;
  const tierOneRemaining = tierBreakdown?.tier_1.remaining_kwh ?? cycle?.tier_1_remaining_kwh ?? null;
  const projectionReady = cycle?.projection && ['available', 'ready'].includes(cycle.projection.status);
  const recoveredEnergy = cycle?.energy_quality?.recovered_kwh ?? cycle?.recovered_gap_energy_kwh ?? cycle?.connection_gap_energy_kwh ?? null;
  const estimatedEnergy = cycle?.estimated_missing_energy_kwh ?? cycle?.energy_quality?.estimated_kwh ?? null;
  const measuredEnergy = cycle?.measured_energy_kwh ?? cycle?.energy_quality?.measured_kwh ?? null;
  const currentUsage = cycle?.current_usage_kwh ?? cycle?.saved_usage_kwh ?? null;
  const tierProgress = cycle && tierBoundary !== null && currentUsage !== null
    ? (() => { const usage = Number(currentUsage); const boundary = Number(tierBoundary); const scale = Math.max(boundary * 1.25, usage * 1.1, 1); return { usage, scale, usagePercent: Math.min(100, usage / scale * 100), boundaryPercent: Math.min(100, boundary / scale * 100) }; })()
    : null;
  const elapsedDays = cycle ? Math.max(0, Math.min(cycleDays ?? 0, Math.ceil((billingClock.getTime() - new Date(cycle.start_utc).getTime()) / 86_400_000))) : null;
  const remainingDays = cycleDays !== null && elapsedDays !== null ? Math.max(0, cycleDays - elapsedDays) : null;

  return <div className={mode === 'billing' ? 'page billing-page' : 'billing-settings-panel'}>
    {mode === 'billing' && <><header className="page-heading"><div><p className="eyebrow">Rates and saved home usage</p><h1>Billing</h1><p>See current-cycle usage, tier progress, costs, projections, and data quality.</p></div><Link className="button button-primary" to="/settings?section=rates">Manage billing settings</Link></header>
    <Notice>These estimates use saved readings from the Main service branch. Main service combines its operator-verified, non-overlapping sensors that together measure the entire home. A bill PDF supplies rate information only and never supplies usage.</Notice>
    <Card title="Current Rate Plan" eyebrow="Rates used for this estimate" className="current-plan-card">
      {account?.current_rate_plan ? <><div className="plan-title"><div><strong>{account.current_rate_plan.name}</strong><span>{account.current_rate_plan.rate_class === 'residential_tiered' ? 'Residential tiered rate' : account.current_rate_plan.rate_class.replaceAll('_', ' ')}</span></div><StatusPill state={account.current_rate_plan.currently_used ? 'current' : 'waiting'} label={account.current_rate_plan.currently_used ? 'Currently used' : 'Not currently used'} /></div><dl><div><dt>Utility</dt><dd>{account.current_rate_plan.utility_name}</dd></div><div><dt>Tier 1</dt><dd>{exactRate(account.current_rate_plan.tier_1_price_per_kwh, 5, '/kWh')}</dd></div><div><dt>Tier 2</dt><dd>{exactRate(account.current_rate_plan.tier_2_price_per_kwh, 5, '/kWh')}</dd></div><div><dt>Daily service charge</dt><dd>{exactRate(account.current_rate_plan.daily_service_charge, 3, '/day')}</dd></div><div><dt>Summer allowance</dt><dd>{account.current_rate_plan.daily_baseline_allowance_kwh === null ? 'Waiting for rate details' : `${Number(account.current_rate_plan.daily_baseline_allowance_kwh)} kWh per billing day`}</dd></div><div><dt>Generation service</dt><dd>{account.current_rate_plan.generation_service ?? 'Not specified by this plan'}</dd></div><div><dt>Effective date</dt><dd>{dateTime(account.current_rate_plan.effective_start)}</dd></div></dl></> : account?.plan_name ? <><div className="plan-title"><div><strong>{account.plan_name}</strong><span>Rate details are not reported by this server version</span></div><StatusPill state="current" /></div><p>Update the server to see tier prices, daily charges, and allowances here.</p></> : <EmptyState title="No current rate plan" detail="Review and apply a rate plan to calculate costs from saved readings." />}
    </Card>
    <Card title="Current Billing Cycle" eyebrow="Main service usage">
      {cycle ? <><div className="plan-title"><div><strong>{dateTime(cycle.start_utc)} – {dateTime(cycle.end_utc)}</strong><span>Billing source: {cycle.service_branch_name ?? account?.home_total_branch?.name ?? 'Main service'}</span></div><StatusPill state={cycle.calculation_state === 'unavailable' || tierLabel(cycle.tier_state, cycle.reading_coverage) === 'Tier not confirmed' ? 'waiting' : 'current'} label={tierLabel(cycle.tier_state, cycle.reading_coverage)} /></div><dl><div><dt>Days elapsed</dt><dd>{elapsedDays ?? '—'}</dd></div><div><dt>Days remaining</dt><dd>{remainingDays ?? '—'}</dd></div><div><dt>Usage to date</dt><dd>{pendingNumeric(currentUsage, 'kWh')}</dd></div><div><dt>Reading coverage</dt><dd>{percent(cycle.reading_coverage === null ? null : Number(cycle.reading_coverage))}</dd></div><div><dt>Measured accepted energy</dt><dd>{pendingNumeric(measuredEnergy, 'kWh')}</dd></div><div><dt>Recovered connection-gap energy</dt><dd>{pendingNumeric(recoveredEnergy, 'kWh')}</dd></div><div><dt>Estimated missing energy</dt><dd>{pendingNumeric(estimatedEnergy, 'kWh')}</dd></div>{cycle.estimated_missing_energy_lower_kwh !== undefined && cycle.estimated_missing_energy_upper_kwh !== undefined && <div><dt>Estimated-energy range</dt><dd>{pendingNumeric(cycle.estimated_missing_energy_lower_kwh, 'kWh')} – {pendingNumeric(cycle.estimated_missing_energy_upper_kwh, 'kWh')}</dd></div>}<div><dt>Unresolved connection gaps</dt><dd>{cycle.unresolved_connection_gap_count ?? '—'}</dd></div><div><dt>Unknown energy</dt><dd>{pendingNumeric(cycle.unknown_energy_kwh ?? cycle.unresolved_energy_kwh ?? null, 'kWh')}</dd></div><div><dt>Calculation state</dt><dd>{cycle.calculation_state?.replaceAll('_', ' ') ?? 'Not reported'}</dd></div><div><dt>Confidence</dt><dd>{cycle.confidence ? `${cycle.confidence[0]!.toUpperCase()}${cycle.confidence.slice(1)}` : 'Not rated'}</dd></div></dl>{cycle.confidence_reasons.length > 0 && <Notice>{cycle.confidence_reasons.join(' ')}</Notice>}{cycle.availability_reasons.map((reason) => <Notice key={reason.code} kind={reason.severity === 'info' ? 'info' : 'warning'}><span data-reason-code={reason.code} data-reason-severity={reason.severity}>{reason.message}</span></Notice>)}{tierLabel(cycle.tier_state, cycle.reading_coverage) === 'Tier not confirmed' && cycle.availability_reasons.length === 0 && <Notice kind="warning">Tier cannot be confirmed because the server reported unresolved billing evidence. Reading coverage alone does not block a tiered calculation when total energy is known.</Notice>}{cycle.energy_quality && <p className="disclosure">Estimated gap energy remains separate from accepted History. Raw History modified: {cycle.energy_quality.raw_history_modified ? 'yes' : 'no'}.</p>}</> : <><p>Billing-cycle progress is not available from this server.</p><dl><div><dt>Usage to date</dt><dd>{numeric(summary?.billing_cycle.energy_kwh === null || summary?.billing_cycle.energy_kwh === undefined ? null : Number(summary.billing_cycle.energy_kwh), 'kWh')}</dd></div><div><dt>Reading coverage</dt><dd>{percent(summary?.billing_cycle.completeness === null || summary?.billing_cycle.completeness === undefined ? null : Number(summary.billing_cycle.completeness))}</dd></div></dl><Notice kind="warning">Tier not confirmed because the server did not return a billing-cycle calculation.</Notice></>}
    </Card>
    <Card title="Tier Breakdown" eyebrow="Server-calculated usage and charges">
      {cycle ? <><dl><div><dt>Tier 1 usage</dt><dd>{pendingNumeric(tierOneUsage, 'kWh')}</dd></div><div><dt>Tier 1 rate</dt><dd>{exactRate(tierOneRate, 5, '/kWh')}</dd></div><div><dt>Tier 1 cost</dt><dd>{pendingMoney(tierOneCost)}</dd></div><div><dt>Tier 2 usage</dt><dd>{pendingNumeric(tierTwoUsage, 'kWh')}</dd></div><div><dt>Tier 2 rate</dt><dd>{exactRate(tierTwoRate, 5, '/kWh')}</dd></div><div><dt>Tier 2 cost</dt><dd>{pendingMoney(tierTwoCost)}</dd></div><div><dt>Tier 1 remaining</dt><dd>{pendingNumeric(tierOneRemaining, 'kWh')}</dd></div><div><dt>Tier 2 begins above</dt><dd>{tierBoundary === null ? 'Waiting for rate details' : `${Number(tierBoundary).toFixed(1)} kWh for this billing cycle`}</dd></div></dl>{tierProgress && <div className="tier-progress"><div className="tier-progress-track" role="progressbar" aria-label="Billing-cycle tier usage" aria-valuemin={0} aria-valuemax={tierProgress.scale} aria-valuenow={tierProgress.usage}><span className="tier-progress-tier-one" style={{ width: `${tierProgress.boundaryPercent}%` }} /><span className="tier-progress-tier-two" style={{ left: `${tierProgress.boundaryPercent}%` }} /><span className="tier-progress-used" style={{ width: `${tierProgress.usagePercent}%` }} /><span className="tier-progress-threshold" style={{ left: `${tierProgress.boundaryPercent}%` }} /></div><div><span>Tier 1</span><strong>Tier 2 begins above {Number(tierBoundary).toFixed(1)} kWh</strong><span>Tier 2</span></div></div>}<p className="disclosure">Tier 1 allowance is the daily baseline allowance multiplied by the number of billing days.</p></> : <EmptyState title="Tier details are not available" detail="A current Main service billing cycle and published rate are required." />}
    </Card>
    <Card title="Cost Summary" eyebrow="Charges to date and full-cycle estimate">
      {cycle ? <><dl><div><dt>Tier 1 energy</dt><dd>{pendingMoney(tierOneCost)}</dd></div><div><dt>Tier 2 energy</dt><dd>{pendingMoney(tierTwoCost)}</dd></div><div><dt>Energy charges to date</dt><dd>{pendingMoney(cycle.estimated_energy_charges)}</dd></div><div><dt>Service charge to date</dt><dd>{pendingMoney(tierBreakdown?.service_charge_to_date ?? cycle.service_charge ?? cycle.estimated_fixed_charges)}</dd></div><div><dt>Cost to date</dt><dd>{pendingMoney(tierBreakdown?.total_to_date ?? cycle.cost_to_date ?? cycle.estimated_total)}</dd></div>{cycle.cost_range && <div><dt>Cost range</dt><dd>{money(cycle.cost_range.lower)} – {money(cycle.cost_range.upper)}</dd></div>}<div><dt>Cost basis</dt><dd>{cycle.cost_basis?.replaceAll('_', ' ') ?? 'Not reported'}</dd></div></dl>{Number(cycle.tou_unallocated_gap_energy_kwh ?? 0) > 0 && <Notice kind="warning">{pendingNumeric(cycle.tou_unallocated_gap_energy_kwh ?? null, 'kWh')} of gap energy cannot be placed into an exact TOU period; measured-period cost remains exact and the gap is estimated separately.</Notice>}{projectionReady && cycle.projection ? <><h3>Estimated full billing cycle</h3><p>Projected total for the current billing cycle</p><dl><div><dt>Projected usage</dt><dd>{pendingNumeric(cycle.projection.projected_usage_kwh ?? null, 'kWh')}</dd></div><div><dt>Projected Tier 1 usage</dt><dd>{pendingNumeric(cycle.projection.projected_tier_1_usage_kwh ?? null, 'kWh')}</dd></div><div><dt>Projected Tier 2 usage</dt><dd>{pendingNumeric(cycle.projection.projected_tier_2_usage_kwh ?? null, 'kWh')}</dd></div><div><dt>Projected Tier 1 cost</dt><dd>{pendingMoney(cycle.projection.projected_tier_1_cost ?? null)}</dd></div><div><dt>Projected Tier 2 cost</dt><dd>{pendingMoney(cycle.projection.projected_tier_2_cost ?? null)}</dd></div><div><dt>Projected service charge</dt><dd>{pendingMoney(cycle.projection.projected_service_charge ?? null)}</dd></div><div><dt>Estimated monthly bill</dt><dd>{pendingMoney(cycle.projection.projected_total ?? null)}</dd></div><div><dt>Confidence</dt><dd>{cycle.projection.confidence ? `${cycle.projection.confidence[0]!.toUpperCase()}${cycle.projection.confidence.slice(1)} confidence` : 'Not rated'}</dd></div></dl>{cycle.projection.confidence_reasons.length > 0 && <p className="disclosure">{cycle.projection.confidence_reasons.join(' ')}</p>}</> : <Notice kind="info">Not enough data to estimate the full bill yet.</Notice>}</> : <EmptyState title="Cost details are not available" detail="Choose a Main service branch and publish a complete rate plan." />}
      {selectedEstimate && <details className="technical-details"><summary>Calculation details</summary><dl><div><dt>Rate version</dt><dd>{selectedEstimate.rate_plan_version_id}</dd></div><div><dt>Missing readings</dt><dd>{selectedEstimate.missing_intervals}</dd></div></dl></details>}
    </Card>
    </>}
    {mode === 'settings' && <>
    <Card title="Rate-plan management" eyebrow="Rates & data sources"><p>Manage the SCE plan library, immutable versions, effective dates, manual rates, and rate-only bill imports here.</p><PermissionGate permission="rates.bill_import"><button type="button" className="button button-primary" onClick={() => setImportOpen(true)}><Upload aria-hidden="true" /> Import rates from SCE bill PDF</button></PermissionGate></Card>
    <PermissionGate permission="rates.view"><RateSourceWorkflow key={`rate-source-workflow-${selectedHomeId}`} homeId={selectedHomeId} accounts={data.accounts.map((entry) => ({ utility_account_id: entry.utility_account_id, plan_name: entry.plan_name }))} /></PermissionGate>
    <Card title="Imported bill rates" eyebrow="Rate information only">
      {data.drafts.length === 0 ? <EmptyState title="No imported bill rates" detail="Import an SCE bill PDF when you need to review rate details. Usage and customer information are discarded." /> : <div className="draft-list">{data.drafts.map((draft) => { const currentlyUsed = draft.resulting_rate_version_id !== null && draft.resulting_rate_version_id === account?.rate_version_id; const label = currentlyUsed ? 'Currently used' : draft.state === 'published' ? 'Published' : 'Draft'; return <button type="button" key={draft.id} onClick={() => { setReview(draft); setImportOpen(true); }}><FileCheck2 aria-hidden="true" /><div><strong>{draft.rate_plan_name ?? 'Unnamed rate'}</strong><span>{draft.utility_name ?? 'Source not identified'} · imported {draft.created_at ? dateTime(draft.created_at) : 'date not reported'}</span></div><StatusPill state={currentlyUsed ? 'current' : draft.state} label={label} /><span>Review</span><ArrowRight aria-hidden="true" /></button>; })}</div>}
    </Card>
    <Dialog open={importOpen} title="Import rates from SCE bill PDF" description="The server extracts reusable prices and threshold rules only; no bill usage becomes History." onClose={closeImport} wide>
      {!review ? <><div className="boundary-panel"><FileLock2 aria-hidden="true" /><div><h3>Rates and reusable cost rules only</h3><p>The document is used only to identify reusable schedule and pricing fields. Customer identity, service details, meter readings, consumption, balances, payments, amount due, bill totals and historical charts are discarded and never shown.</p><p>No upload can create or change sensor readings, intervals, History, completeness, calibration, forecasts, energy totals or costs until a separately reviewed rate version is published and assigned.</p></div></div><form ref={formRef} className="upload-form" onSubmit={submitPdf}><div className="file-drop"><Upload aria-hidden="true" /><label htmlFor="rate-document">Choose an SCE PDF rate source</label><input id="rate-document" name="rateDocument" type="file" accept="application/pdf,.pdf" required aria-describedby="upload-limits" /><small id="upload-limits">PDF only · maximum 10 MiB · server-enforced page and processing limits · no cloud OCR</small></div>{upload.isError && <p className="form-error" role="alert">{upload.error instanceof Error ? upload.error.message : 'The document could not be processed.'}</p>}<div className="dialog-actions"><button type="button" className="button button-secondary" onClick={closeImport}>Cancel</button><button type="submit" className="button button-primary" disabled={upload.isPending}>{upload.isPending ? 'Extracting allowed rates…' : 'Create rate draft'}</button></div></form></> : <form className="rate-review" onSubmit={submitCorrections}>
        <Notice kind={review.candidate_complete ? 'success' : 'warning'}><strong>{review.candidate_complete ? 'Tiered rate and summer baseline were extracted successfully.' : 'Review required.'}</strong> Parsing never activates, publishes or assigns a rate; an administrator must still confirm the source and effective date.</Notice>
        <div className="review-meta"><div><span>Utility</span><strong>{review.utility_name ?? 'Not identified'}</strong></div><div><span>Plan</span><strong>{review.rate_plan_name ?? 'Not identified'}</strong></div><div><span>Structure</span><strong>{review.plan_classification.replaceAll('_', ' ')}</strong></div><div><span>Holiday treatment</span><strong>{review.holiday_treatment.replaceAll('_', ' ')}</strong></div><div><span>Evidence period</span><strong>{review.billing_period_start && review.billing_period_end ? `${review.billing_period_start} – ${review.billing_period_end}` : 'Not supplied'}</strong></div></div>
        <details className="technical-details"><summary>Technical details</summary><dl><div><dt>Parser</dt><dd>{review.parser_version}</dd></div><div><dt>Source SHA-256</dt><dd><code title={review.artifact_sha256}>{review.artifact_sha256.slice(0, 18)}…</code></dd></div></dl></details>
        <div className="review-meta" aria-label="Operational rate preview"><div><span>Rate type</span><strong>{review.rate_class === 'residential_tiered' ? 'Residential tiered' : review.rate_class?.replaceAll('_', ' ') ?? 'Not identified'}</strong></div><div><span>Season</span><strong>{review.tier_threshold_rule?.season === 'summer' ? 'Summer' : 'Review required'}</strong></div><div><span>Generation service</span><strong>{review.cca_or_direct_access_indicator === 'sce_generation' ? 'SCE generation service' : review.cca_or_direct_access_indicator?.replaceAll('_', ' ') ?? 'Not identified'}</strong></div><div><span>Base daily charge</span><strong>{evidenceValue(review, 'Base services charge')}</strong></div><div><span>Tier 1 all-in</span><strong>{evidenceValue(review, 'Tier 1 all-in rate')}</strong></div><div><span>Tier 2 all-in</span><strong>{evidenceValue(review, 'Tier 2 all-in rate')}</strong></div><div><span>Summer baseline allowance</span><strong>{review.tier_threshold_rule ? `${decimalText(review.tier_threshold_rule.source_allowance_kwh)} kWh` : 'Review required'}</strong></div><div><span>Billing days</span><strong>{review.tier_threshold_rule?.source_billing_days ?? review.billing_period_days ?? 'Review required'}</strong></div><div><span>Daily baseline allowance</span><strong>{review.tier_threshold_rule?.kwh_per_day !== null && review.tier_threshold_rule?.kwh_per_day !== undefined ? `${decimalText(review.tier_threshold_rule.kwh_per_day)} kWh/day` : 'Review required'}</strong></div><div><span>Tier 2 starts above</span><strong>{review.tier_threshold_rule?.source_billing_days ? `${decimalText(review.tier_threshold_rule.source_allowance_kwh)} kWh for this ${review.tier_threshold_rule.source_billing_days}-day bill` : 'Review required'}</strong></div><div><span>Active comparison</span><strong>{activeRateComparison(review, currentRate)}</strong></div></div>
        <section aria-labelledby="bill-rate-evidence-title"><h3 id="bill-rate-evidence-title">Extracted reusable rate evidence</h3><div className="rate-evidence-list">{review.source_evidence.map((field, index) => <div key={`${field.name ?? field.field ?? 'field'}-${index}`}><span>{field.supporting_label ?? field.label ?? field.name ?? 'Rate evidence'}</span><strong>{field.normalized_value ?? field.label ?? 'Recorded'}</strong></div>)}</div></section>
        {review.tier_threshold_rule && <Notice>The summer allowance is stored as {decimalText(review.tier_threshold_rule.kwh_per_day)} kWh per billing day, so the Tier 1 boundary adjusts to the actual number of days in each billing cycle. The source bill’s dates remain evidence metadata, not tariff effective dates.</Notice>}
        {review.publication_scope === 'review_only' && <Notice kind="warning"><strong>The operational threshold is incomplete.</strong> Enter the missing billing-day count when the source allowance is present. No day count, threshold, winter rate or effective date is invented.</Notice>}
        <div className="rate-field-list">{correctionFields.map((field) => <div className="rate-field" key={field.key}><div className="field"><label htmlFor={`rate-${field.key}`}>{field.label}</label><input id={`rate-${field.key}`} name={field.key} type={field.key === 'billing_period_days' ? 'number' : 'text'} min={field.key === 'billing_period_days' ? 1 : undefined} max={field.key === 'billing_period_days' ? 62 : undefined} defaultValue={draftValue(review, field.key)} /></div><div className="field-evidence"><span>Accepted rate field</span><span>{review.source_evidence.length} source evidence item{review.source_evidence.length === 1 ? '' : 's'}</span></div></div>)}</div>
        {correct.isError && <p className="form-error" role="alert">{correct.error instanceof Error ? correct.error.message : 'Corrections could not be saved.'}</p>}
        <div className="dialog-actions"><button type="button" className="button button-secondary" onClick={closeImport}>Close review</button><PermissionGate permission="rates.manage"><button type="button" className="button button-danger" onClick={() => setDeleteDraftOpen(true)}><Trash2 aria-hidden="true" /> Delete draft</button></PermissionGate><button type="submit" className="button button-secondary" disabled={correct.isPending}>{correct.isPending ? 'Saving…' : 'Save corrections'}</button><PermissionGate permission="rates.manage"><button type="button" className="button button-primary" disabled={review.publication_scope === 'review_only'} title={review.publication_scope === 'review_only' ? 'Safely bounded or complete tariff evidence is required before publication' : undefined} onClick={() => setPublishOpen(true)}>Publish version</button></PermissionGate></div>
      </form>}
    </Dialog>
    <ConfirmDialog open={publishOpen} title="Publish and assign an immutable rate version?" description={<div><p>Publishing is separate from extraction. This version can affect estimates only within an administrator-confirmed effective range and only when assigned to sensor-derived intervals.</p><div className="field"><label htmlFor="rate-effective-at">Effective date and time</label><input id="rate-effective-at" type="datetime-local" value={publishAt} onChange={(event) => setPublishAt(event.target.value)} required /></div></div>} confirmLabel="Publish rate version" busy={publish.isPending} confirmDisabled={!publishAt} onCancel={() => setPublishOpen(false)} onConfirm={() => { if (publishAt) publish.mutate(); }} tone="warning" />
    <ConfirmDialog open={deleteDraftOpen} title="Delete this PDF rate draft?" description="The extracted working draft and its correction records will be permanently removed. Original PDF bytes were already discarded. Any separately published immutable rate version remains available for assigned cost calculations." confirmLabel="Delete rate draft" busy={removeDraft.isPending} onCancel={() => setDeleteDraftOpen(false)} onConfirm={() => removeDraft.mutate()} tone="danger" />
    </>}
  </div>;
}
