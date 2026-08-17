import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, FileCheck2, Plus, RefreshCw, ShieldCheck, Trash2 } from 'lucide-react';
import { useMemo, useState, type FormEvent } from 'react';
import { api, type ManualRateCandidateInput, type ManualRatePeriodInput } from '../api';
import type { RateCandidate, RateCandidates, RateCandidateWorkflow } from '../api/schemas';
import { PermissionGate } from '../auth/PermissionGate';
import { Card, ConfirmDialog, Dialog, EmptyState, ErrorState, Loading, Notice, StatusPill } from '../components/ui';
import { dateTime } from '../lib/format';
import { formString } from '../lib/form';

const RATE_TIMEZONE = 'America/Los_Angeles';
const DECIMAL_PATTERN = String.raw`\d{1,3}(?:\.\d{1,8})?`;

interface UtilityAccountOption {
  utility_account_id: string;
  plan_name: string | null;
}

function rateStatusKey(homeId: string) {
  return ['rate-source-status', homeId] as const;
}

function rateCandidatesKey(homeId: string) {
  return ['rate-source-candidates', homeId] as const;
}

function shortHash(value: string): string {
  return `${value.slice(0, 16)}…`;
}

function localDate(value: string | null | undefined): string {
  if (!value) return '';
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: RATE_TIMEZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date(value));
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((entry) => entry.type === type)?.value ?? '';
  return `${part('year')}-${part('month')}-${part('day')}`;
}

function homeMidnight(date: string): string {
  const [year, month, day] = date.split('-').map(Number);
  if (!year || !month || !day) throw new Error('Choose a valid effective date.');
  const desired = Date.UTC(year, month - 1, day, 0, 0, 0);
  let instant = desired + 8 * 60 * 60 * 1000;
  const formatter = new Intl.DateTimeFormat('en-US', {
    timeZone: RATE_TIMEZONE,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  });
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const parts = formatter.formatToParts(new Date(instant));
    const number = (type: Intl.DateTimeFormatPartTypes) => Number(parts.find((entry) => entry.type === type)?.value);
    const observed = Date.UTC(number('year'), number('month') - 1, number('day'), number('hour'), number('minute'), number('second'));
    const adjustment = desired - observed;
    instant += adjustment;
    if (adjustment === 0) break;
  }
  return new Date(instant).toISOString();
}

function checkResult(result: { state: 'review_required' | 'unchanged' | 'failed'; run_id: string; candidate_id: string | null; error_code: string | null }) {
  if (result.state === 'failed') return <Notice kind="warning"><strong>Check completed with a failure.</strong> {result.error_code ?? 'The source could not be validated.'} No candidate or active rate was changed; any existing last-known-good evidence was left unchanged.</Notice>;
  if (result.state === 'unchanged') return <Notice><strong>Check completed.</strong> The verified source is unchanged. Run {result.run_id} did not create or activate a rate.</Notice>;
  return <Notice kind="success"><strong>Check completed.</strong> Candidate {result.candidate_id ?? 'identifier unavailable'} requires explicit review. Nothing was published or activated.</Notice>;
}

export function RateSourceStatusCard({ homeId }: { homeId: string }) {
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: rateStatusKey(homeId), queryFn: () => api.rateSourceStatus(homeId), enabled: Boolean(homeId), refetchInterval: 60_000 });
  const check = useMutation({
    mutationFn: () => api.checkRates(homeId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: rateStatusKey(homeId) });
      void queryClient.invalidateQueries({ queryKey: rateCandidatesKey(homeId) });
    },
  });

  return <Card title="Official SCE source" eyebrow="Allowlisted synchronous validation" className="source-card">
    {status.isLoading ? <Loading label="Loading exact-home source status" /> : status.isError ? <ErrorState error={status.error} retry={() => void status.refetch()} /> : status.data ? <>
      <div className="source-state"><ShieldCheck aria-hidden="true" /><div><strong>{status.data.scheduled.state === 'not_configured' ? 'Southern California Edison' : status.data.scheduled.source_name}</strong><StatusPill state={status.data.last_run?.state ?? status.data.scheduled.state} /></div></div>
      <dl className="source-evidence-list">
        <div><dt>Scheduled source</dt><dd>{status.data.scheduled.state.replaceAll('_', ' ')}</dd></div>
        <div><dt>Last run</dt><dd>{status.data.last_run ? `${dateTime(status.data.last_run.completed_at)} · ${status.data.last_run.event_code}` : 'Never checked'}</dd></div>
        <div><dt>Last run source</dt><dd>{status.data.last_run ? `${status.data.last_run.source_name ?? 'Name unavailable'} · ${status.data.last_run.source_type ?? 'type unavailable'} · ${status.data.last_run.source_url ?? 'URL unavailable'}` : 'Never checked'}</dd></div>
        <div><dt>Last success</dt><dd>{status.data.last_success ? `${dateTime(status.data.last_success.completed_at)} · ${status.data.last_success.state.replaceAll('_', ' ')}` : 'No verified run'}</dd></div>
        <div><dt>Last failure</dt><dd>{status.data.last_failure ? `${dateTime(status.data.last_failure.completed_at)} · ${status.data.last_failure.error_code ?? status.data.last_failure.event_code}` : 'None recorded'}</dd></div>
        <div><dt>Active rate</dt><dd>{status.data.active.state === 'active' ? `${status.data.active.plan_name} · ${dateTime(status.data.active.effective_start)} to ${status.data.active.effective_end ? dateTime(status.data.active.effective_end) : 'open-ended'}` : 'Not configured'}</dd></div>
        <div><dt>Active provenance</dt><dd>{status.data.active.state === 'active' ? `${status.data.active.provenance.source_name ?? status.data.active.provenance.origin} · ${status.data.active.provenance.source_url ?? status.data.active.provenance.origin} · ${shortHash(status.data.active.provenance.source_artifact_sha256)}` : 'Unavailable'}</dd></div>
        <div><dt>Last known good</dt><dd>{status.data.last_known_good.state === 'available' ? `${status.data.last_known_good.source_name} · ${dateTime(status.data.last_known_good.retrieved_at)} · ${shortHash(status.data.last_known_good.source_artifact_sha256)}${status.data.last_known_good.active_source_match ? ' · active match' : ''}` : 'Unavailable'}</dd></div>
      </dl>
      <PermissionGate permission="rates.sync"><button type="button" className="button button-secondary" onClick={() => check.mutate()} disabled={check.isPending}><RefreshCw className={check.isPending ? 'spin' : ''} aria-hidden="true" /> {check.isPending ? 'Checking official source…' : 'Check now'}</button></PermissionGate>
      {check.data && checkResult(check.data)}
      {check.isError && <Notice kind="warning"><strong>Check request failed.</strong> {check.error instanceof Error ? check.error.message : 'The source check could not run.'} No success was recorded.</Notice>}
    </> : null}
  </Card>;
}

export function RateSourceWorkflow({ homeId, accounts }: { homeId: string; accounts: UtilityAccountOption[] }) {
  const queryClient = useQueryClient();
  const [selectedCandidateId, setSelectedCandidateId] = useState('');
  const [manualOpen, setManualOpen] = useState(false);
  const [manualMessage, setManualMessage] = useState('');
  const candidates = useQuery({ queryKey: rateCandidatesKey(homeId), queryFn: () => api.rateSourceCandidates(homeId), enabled: Boolean(homeId), refetchInterval: 60_000 });
  const selected = candidates.data?.candidates.find((candidate) => candidate.id === selectedCandidateId);

  function updateWorkflow(candidateId: string, workflow: RateCandidateWorkflow) {
    queryClient.setQueryData<RateCandidates>(rateCandidatesKey(homeId), (current) => current ? {
      ...current,
      candidates: current.candidates.map((candidate) => candidate.id === candidateId ? { ...candidate, workflow } : candidate),
    } : current);
    void queryClient.invalidateQueries({ queryKey: rateCandidatesKey(homeId) });
    void queryClient.invalidateQueries({ queryKey: rateStatusKey(homeId) });
  }

  return <>
    <Card title="SCE rate candidates" eyebrow="Review, publish, then activate for an exact account" action={<PermissionGate permission="rates.manage"><button type="button" className="button button-secondary" onClick={() => { setManualMessage(''); setManualOpen(true); }}><Plus aria-hidden="true" /> Manual fallback</button></PermissionGate>}>
      <Notice><strong>Rate facts only.</strong> These candidates contain reusable schedules, exact prices, effective dates and provenance. They never contain customer identity, bill usage, readings, totals, balances or payment data.</Notice>
      {manualMessage && <Notice kind="success">{manualMessage}</Notice>}
      {candidates.isLoading ? <Loading label="Loading exact-home rate candidates" /> : candidates.isError ? <ErrorState error={candidates.error} retry={() => void candidates.refetch()} /> : candidates.data?.candidates.length ? <div className="draft-list rate-candidate-list">{candidates.data.candidates.map((candidate) => {
        const names = candidate.normalized_rates.plans.map((plan) => plan.rate_plan_name).join(', ');
        const manual = candidate.validation_evidence.origin === 'manual_administrator_entry';
        return <button type="button" key={candidate.id} onClick={() => setSelectedCandidateId(candidate.id)} aria-label={`Open ${manual ? 'manual' : 'official'} rate candidate ${candidate.id}`}><FileCheck2 aria-hidden="true" /><div><strong>{names}</strong><span>{manual ? 'Manual official-source entry' : 'Official HTTPS source'} · retrieved {dateTime(candidate.source.retrieved_at)} · {shortHash(candidate.source.artifact_sha256)}</span></div><StatusPill state={candidate.workflow.state} /><ArrowRight aria-hidden="true" /></button>;
      })}</div> : <EmptyState title="No SCE candidates for this home" detail="Run an official source check or create a manual candidate from verified official SCE rate facts." />}
    </Card>
    <Dialog open={Boolean(selectedCandidateId)} title="Review SCE rate candidate" description="Advancement is scoped to the active home and requires separate review, publication and account activation." onClose={() => setSelectedCandidateId('')} wide>
      {selected ? <CandidateReview candidate={selected} homeId={homeId} accounts={accounts} onWorkflow={updateWorkflow} onClose={() => setSelectedCandidateId('')} /> : candidates.isLoading || candidates.isFetching ? <Loading label="Loading selected candidate" /> : <ErrorState error={new Error('The selected candidate is no longer available for this home.')} />}
    </Dialog>
    <ManualCandidateDialog open={manualOpen} homeId={homeId} onClose={() => setManualOpen(false)} onCreated={(candidateId, created) => {
      setManualOpen(false);
      setManualMessage(created ? 'Manual candidate created for this home. It still requires review.' : 'The identical manual candidate already exists for this home; no duplicate was created.');
      setSelectedCandidateId(candidateId);
      void queryClient.invalidateQueries({ queryKey: rateCandidatesKey(homeId) });
      void queryClient.invalidateQueries({ queryKey: rateStatusKey(homeId) });
    }} />
  </>;
}

function CandidateReview({ candidate, homeId, accounts, onWorkflow, onClose }: { candidate: RateCandidate; homeId: string; accounts: UtilityAccountOption[]; onWorkflow: (candidateId: string, workflow: RateCandidateWorkflow) => void; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [planName, setPlanName] = useState(candidate.workflow.selected_plan_name ?? candidate.normalized_rates.plans[0]?.rate_plan_name ?? '');
  const [rejectOpen, setRejectOpen] = useState(false);
  const [publishOpen, setPublishOpen] = useState(false);
  const [activateOpen, setActivateOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [accountId, setAccountId] = useState(candidate.workflow.utility_account_id ?? accounts[0]?.utility_account_id ?? '');
  const selectedPlan = candidate.normalized_rates.plans.find((plan) => plan.rate_plan_name === planName);
  const completeCoverage = candidate.validation_evidence.coverage === 'complete';
  const review = useMutation({
    mutationFn: (payload: Parameters<typeof api.reviewRateCandidate>[2]) => api.reviewRateCandidate(homeId, candidate.id, payload),
    onSuccess: (response) => onWorkflow(candidate.id, response.workflow),
  });
  const publish = useMutation({
    mutationFn: () => api.publishRateCandidate(homeId, candidate.id),
    onSuccess: (response) => { setPublishOpen(false); onWorkflow(candidate.id, response.workflow); },
  });
  const reject = useMutation({
    mutationFn: () => api.rejectRateCandidate(homeId, candidate.id),
    onSuccess: (response) => { setRejectOpen(false); onWorkflow(candidate.id, response.workflow); },
    onError: () => setRejectOpen(false),
  });
  const activate = useMutation({
    mutationFn: () => api.activateRateCandidate(homeId, candidate.id, accountId),
    onSuccess: (response) => {
      setActivateOpen(false);
      onWorkflow(candidate.id, response.workflow);
      void queryClient.invalidateQueries({ queryKey: ['billing', homeId] });
      void queryClient.invalidateQueries({ queryKey: ['home', homeId] });
    },
  });
  const remove = useMutation({
    mutationFn: () => api.deleteRateCandidate(homeId, candidate.id),
    onSuccess: () => {
      setDeleteOpen(false);
      onClose();
      void queryClient.invalidateQueries({ queryKey: rateCandidatesKey(homeId) });
      void queryClient.invalidateQueries({ queryKey: rateStatusKey(homeId) });
    },
    onError: () => setDeleteOpen(false),
  });

  function submitReview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const start = formString(form, 'effectiveStart');
    const end = formString(form, 'effectiveEnd');
    if (form.get('confirmEffective') !== 'on' || form.get('confirmProvenance') !== 'on') return;
    review.mutate({
      selected_plan_name: formString(form, 'planName'),
      effective_start: homeMidnight(start),
      ...(end ? { effective_end: homeMidnight(end) } : {}),
      administrator_confirmed_effective_date: true,
      administrator_confirmed_provenance: true,
    });
  }

  const workflowError = review.error ?? reject.error ?? publish.error ?? activate.error ?? remove.error;
  const canReject = candidate.workflow.state === 'review_required' || candidate.workflow.state === 'reviewed';
  function openReject() { reject.reset(); setRejectOpen(true); }
  function cancelReject() { setRejectOpen(false); reject.reset(); }
  return <div className="candidate-review">
    <Notice kind="warning"><strong>Manual approval required.</strong> Validate the selected plan, exact effective range and recorded provenance against an official SCE source. Review alone does not publish or activate anything.</Notice>
    {!completeCoverage && <Notice><strong>Additional baseline evidence required.</strong> The source proves reusable prices but not the exact account baseline threshold. This candidate remains visible for review and comparison but cannot advance until a complete official or manual schedule is supplied.</Notice>}
    <div className="review-meta"><div><span>Source</span><strong>{candidate.source.name}</strong></div><div><span>Parser</span><strong>{candidate.source.parser_version}</strong></div><div><span>Retrieved</span><strong>{dateTime(candidate.source.retrieved_at)}</strong></div><div><span>Artifact SHA-256</span><code title={candidate.source.artifact_sha256}>{shortHash(candidate.source.artifact_sha256)}</code></div></div>
    {candidate.source.url && <p className="source-provenance"><strong>Recorded source URL:</strong> <code>{candidate.source.url}</code></p>}
    <div className="candidate-plan-summary">
      {candidate.normalized_rates.plans.map((plan) => <article key={plan.rate_plan_name}><strong>{plan.rate_plan_name}</strong><span>{plan.rate_class} · {plan.periods.length} validated periods</span><small>Daily fixed {plan.daily_fixed_charge} USD · monthly fixed {plan.monthly_fixed_charge} USD · baseline credit {plan.baseline_credit_per_kwh} USD/kWh</small></article>)}
    </div>
    {candidate.workflow.state === 'review_required' && <form className="rate-workflow-form" onSubmit={submitReview}>
      <div className="field"><label htmlFor={`candidate-plan-${candidate.id}`}>Validated rate plan</label><select id={`candidate-plan-${candidate.id}`} name="planName" value={planName} onChange={(event) => setPlanName(event.target.value)} required>{candidate.normalized_rates.plans.map((plan) => <option key={plan.rate_plan_name} value={plan.rate_plan_name}>{plan.rate_plan_name}</option>)}</select></div>
      {selectedPlan && <div className="period-summary" aria-label="Selected plan schedule">{selectedPlan.periods.map((period, index) => <span key={`${period.season}-${period.day_type}-${period.start_minute}-${index}`}>{period.season} · {period.day_type} · {String(Math.floor(period.start_minute / 60)).padStart(2, '0')}:{String(period.start_minute % 60).padStart(2, '0')}–{period.end_minute === 1440 ? '24:00' : `${String(Math.floor(period.end_minute / 60)).padStart(2, '0')}:${String(period.end_minute % 60).padStart(2, '0')}`} · {period.price_per_kwh} USD/kWh</span>)}</div>}
      <div className="rate-date-grid"><div className="field"><label htmlFor={`candidate-effective-start-${candidate.id}`}>Effective start date</label><input id={`candidate-effective-start-${candidate.id}`} name="effectiveStart" type="date" defaultValue={localDate(candidate.workflow.effective_start ?? candidate.normalized_rates.effective_start)} required /></div><div className="field"><label htmlFor={`candidate-effective-end-${candidate.id}`}>Effective end date (optional)</label><input id={`candidate-effective-end-${candidate.id}`} name="effectiveEnd" type="date" defaultValue={localDate(candidate.workflow.effective_end ?? candidate.normalized_rates.effective_end)} /></div></div>
      <small>Dates are submitted as midnight in {RATE_TIMEZONE}; the server stores authoritative UTC instants.</small>
      <label className="workflow-confirm"><input name="confirmEffective" type="checkbox" required /> I confirmed this exact effective range against the official source.</label>
      <label className="workflow-confirm"><input name="confirmProvenance" type="checkbox" required /> I confirmed the recorded source and artifact provenance.</label>
      <div className="dialog-actions"><button type="button" className="button button-secondary" onClick={onClose}>Close</button><PermissionGate permission="rates.manage"><button type="button" className="button button-danger" onClick={() => setDeleteOpen(true)}><Trash2 aria-hidden="true" /> Delete candidate</button><button type="button" className="button button-danger" onClick={openReject}>Reject candidate</button><button type="submit" className="button button-primary" disabled={review.isPending || !completeCoverage} title={completeCoverage ? undefined : 'Complete reusable schedule evidence is required'}>{review.isPending ? 'Recording review…' : 'Confirm candidate review'}</button></PermissionGate></div>
    </form>}
    {candidate.workflow.state === 'reviewed' && <Notice kind="success"><strong>Review recorded.</strong> Effective {dateTime(candidate.workflow.effective_start)} to {dateTime(candidate.workflow.effective_end)}. This candidate is not published or active.</Notice>}
    {candidate.workflow.state === 'published' && <><Notice kind="success"><strong>Immutable version published.</strong> It does not affect costs until assigned to one exact utility account.</Notice><div className="field"><label htmlFor={`candidate-account-${candidate.id}`}>Activation utility account</label><select id={`candidate-account-${candidate.id}`} value={accountId} onChange={(event) => setAccountId(event.target.value)} required><option value="">Choose an exact account</option>{accounts.map((account) => <option key={account.utility_account_id} value={account.utility_account_id}>{account.plan_name ?? 'Unassigned account'} ({account.utility_account_id})</option>)}</select></div></>}
    {candidate.workflow.state === 'activated' && <Notice kind="success"><strong>Active for account {candidate.workflow.utility_account_id}.</strong> Sensor-derived intervals may now use immutable rate version {candidate.workflow.rate_plan_version_id} within its effective range.</Notice>}
    {candidate.workflow.state === 'rejected' && <Notice kind="warning">This candidate was rejected and cannot advance.</Notice>}
    {workflowError && <Notice kind="warning"><strong>Workflow action failed.</strong> {workflowError instanceof Error ? workflowError.message : 'The candidate was not advanced.'}</Notice>}
    {candidate.workflow.state !== 'review_required' && <div className="dialog-actions"><button type="button" className="button button-secondary" onClick={onClose}>Close</button>{candidate.workflow.state === 'rejected' && <PermissionGate permission="rates.manage"><button type="button" className="button button-danger" onClick={() => setDeleteOpen(true)}><Trash2 aria-hidden="true" /> Delete candidate</button></PermissionGate>}{canReject && <PermissionGate permission="rates.manage"><button type="button" className="button button-danger" onClick={openReject}>Reject candidate</button></PermissionGate>}{candidate.workflow.state === 'reviewed' && <PermissionGate permission="rates.manage"><button type="button" className="button button-primary" onClick={() => setPublishOpen(true)}>Publish reviewed version</button></PermissionGate>}{candidate.workflow.state === 'published' && <PermissionGate permission="rates.manage"><button type="button" className="button button-primary" onClick={() => setActivateOpen(true)} disabled={!accountId}>Activate for selected account</button></PermissionGate>}</div>}
    <ConfirmDialog open={rejectOpen} title="Reject this rate candidate?" description={<p>Candidate <code>{candidate.id}</code> will become terminal for home <code>{homeId}</code>. It cannot then be reviewed, published or activated.</p>} confirmLabel="Reject candidate permanently" busy={reject.isPending} onCancel={cancelReject} onConfirm={() => reject.mutate()} tone="warning" />
    <ConfirmDialog open={publishOpen} title="Publish this reviewed rate version?" description={<p>Publication creates an immutable effective-dated version. It remains inactive until a separate account assignment.</p>} confirmLabel="Publish immutable version" busy={publish.isPending} onCancel={() => setPublishOpen(false)} onConfirm={() => publish.mutate()} tone="warning" />
    <ConfirmDialog open={activateOpen} title="Activate this version for the selected account?" description={<p>Only account <code>{accountId}</code> will receive this version for its reviewed effective range. Usage remains authenticated sensor evidence only.</p>} confirmLabel="Activate exact account" busy={activate.isPending} confirmDisabled={!accountId} onCancel={() => setActivateOpen(false)} onConfirm={() => activate.mutate()} tone="warning" />
    <ConfirmDialog open={deleteOpen} title="Delete this disposable rate candidate?" description="Unpublished candidate values and any rejected review record will be permanently removed. Published or activated rate provenance cannot be deleted." confirmLabel="Delete candidate" busy={remove.isPending} onCancel={() => setDeleteOpen(false)} onConfirm={() => remove.mutate()} tone="danger" />
  </div>;
}

interface ManualPeriodDraft extends ManualRatePeriodInput { key: number }
let nextPeriodKey = 1;

function defaultPeriod(): ManualPeriodDraft {
  return { key: nextPeriodKey++, season: 'all', day_type: 'all', period_name: 'all_day', start_minute: 0, end_minute: 1440, price_per_kwh: '0.00000001' };
}

function ManualCandidateDialog({ open, homeId, onClose, onCreated }: { open: boolean; homeId: string; onClose: () => void; onCreated: (candidateId: string, created: boolean) => void }) {
  const [periods, setPeriods] = useState<ManualPeriodDraft[]>([defaultPeriod()]);
  const create = useMutation({ mutationFn: (payload: ManualRateCandidateInput) => api.createManualRateCandidate(homeId, payload), onSuccess: (response) => onCreated(response.candidate_id, response.created) });
  const periodList = useMemo(() => periods.map((period) => ({
    season: period.season,
    day_type: period.day_type,
    period_name: period.period_name,
    start_minute: period.start_minute,
    end_minute: period.end_minute,
    price_per_kwh: period.price_per_kwh,
  })), [periods]);

  function close() {
    create.reset();
    setPeriods([defaultPeriod()]);
    onClose();
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    if (form.get('officialAttestation') !== 'on') return;
    const sourceUrl = formString(form, 'sourceUrl');
    const end = formString(form, 'effectiveEnd');
    create.mutate({
      source_title: formString(form, 'sourceTitle'),
      tariff_identifier: formString(form, 'tariffIdentifier'),
      ...(sourceUrl ? { source_url: sourceUrl } : {}),
      administrator_attests_official_source: true,
      rate_plan_name: formString(form, 'ratePlanName'),
      rate_class: formString(form, 'rateClass'),
      effective_start: homeMidnight(formString(form, 'effectiveStart')),
      ...(end ? { effective_end: homeMidnight(end) } : {}),
      daily_fixed_charge: formString(form, 'dailyFixed'),
      monthly_fixed_charge: formString(form, 'monthlyFixed'),
      baseline_credit_per_kwh: formString(form, 'baselineCredit'),
      periods: periodList,
    });
  }

  function updatePeriod(key: number, update: Partial<ManualPeriodDraft>) {
    setPeriods((current) => current.map((period) => period.key === key ? { ...period, ...update } : period));
  }

  return <Dialog open={open} title="Create manual SCE rate candidate" description="Fallback for exact rate facts verified from an official SCE source; no network fetch is performed." onClose={close} wide>
    <Notice kind="warning"><strong>This is not a guess-and-activate shortcut.</strong> Enter a complete schedule and official provenance. The result still requires review, publication and exact-account activation.</Notice>
    <form className="manual-rate-form" onSubmit={submit}>
      <div className="rate-date-grid"><div className="field"><label htmlFor="manual-source-title">Official source title</label><input id="manual-source-title" name="sourceTitle" required minLength={3} maxLength={160} /></div><div className="field"><label htmlFor="manual-tariff-id">Tariff identifier</label><input id="manual-tariff-id" name="tariffIdentifier" required minLength={2} maxLength={120} /></div></div>
      <div className="field"><label htmlFor="manual-source-url">Official SCE HTTPS URL (optional)</label><input id="manual-source-url" name="sourceUrl" type="url" placeholder="https://www.sce.com/..." /></div>
      <div className="rate-date-grid"><div className="field"><label htmlFor="manual-plan-name">Rate plan name</label><input id="manual-plan-name" name="ratePlanName" required maxLength={120} /></div><div className="field"><label htmlFor="manual-rate-class">Rate class</label><input id="manual-rate-class" name="rateClass" defaultValue="residential" required maxLength={80} /></div><div className="field"><label htmlFor="manual-effective-start">Effective start date</label><input id="manual-effective-start" name="effectiveStart" type="date" required /></div><div className="field"><label htmlFor="manual-effective-end">Effective end date (optional)</label><input id="manual-effective-end" name="effectiveEnd" type="date" /></div></div>
      <div className="rate-charge-grid"><div className="field"><label htmlFor="manual-daily-fixed">Daily fixed charge (USD)</label><input id="manual-daily-fixed" name="dailyFixed" inputMode="decimal" pattern={DECIMAL_PATTERN} defaultValue="0.00000000" required /></div><div className="field"><label htmlFor="manual-monthly-fixed">Monthly fixed charge (USD)</label><input id="manual-monthly-fixed" name="monthlyFixed" inputMode="decimal" pattern={DECIMAL_PATTERN} defaultValue="0.00000000" required /></div><div className="field"><label htmlFor="manual-baseline-credit">Baseline credit (USD/kWh)</label><input id="manual-baseline-credit" name="baselineCredit" inputMode="decimal" pattern={DECIMAL_PATTERN} defaultValue="0.00000000" required /></div></div>
      <fieldset className="manual-periods"><legend>Complete non-overlapping rate schedule</legend>{periods.map((period, index) => <div className="manual-period" key={period.key}><div className="field"><label htmlFor={`manual-season-${period.key}`}>Season {index + 1}</label><select id={`manual-season-${period.key}`} value={period.season} onChange={(event) => updatePeriod(period.key, { season: event.target.value as ManualPeriodDraft['season'] })}><option value="all">All</option><option value="summer">Summer</option><option value="winter">Winter</option></select></div><div className="field"><label htmlFor={`manual-day-${period.key}`}>Day type</label><select id={`manual-day-${period.key}`} value={period.day_type} onChange={(event) => updatePeriod(period.key, { day_type: event.target.value as ManualPeriodDraft['day_type'] })}><option value="all">All</option><option value="weekday">Weekday</option><option value="weekend">Weekend</option><option value="holiday">Holiday</option></select></div><div className="field"><label htmlFor={`manual-name-${period.key}`}>Period name</label><input id={`manual-name-${period.key}`} value={period.period_name} pattern="[A-Za-z0-9_-]+" maxLength={40} onChange={(event) => updatePeriod(period.key, { period_name: event.target.value })} required /></div><div className="field"><label htmlFor={`manual-start-${period.key}`}>Start minute</label><input id={`manual-start-${period.key}`} type="number" min={0} max={1439} value={period.start_minute} onChange={(event) => updatePeriod(period.key, { start_minute: event.target.valueAsNumber })} required /></div><div className="field"><label htmlFor={`manual-end-${period.key}`}>End minute</label><input id={`manual-end-${period.key}`} type="number" min={1} max={1440} value={period.end_minute} onChange={(event) => updatePeriod(period.key, { end_minute: event.target.valueAsNumber })} required /></div><div className="field"><label htmlFor={`manual-price-${period.key}`}>USD per kWh</label><input id={`manual-price-${period.key}`} inputMode="decimal" pattern={DECIMAL_PATTERN} value={period.price_per_kwh} onChange={(event) => updatePeriod(period.key, { price_per_kwh: event.target.value })} required /></div><button type="button" className="icon-button" aria-label={`Remove rate period ${index + 1}`} onClick={() => setPeriods((current) => current.filter((entry) => entry.key !== period.key))} disabled={periods.length === 1}><Trash2 aria-hidden="true" /></button></div>)}</fieldset>
      <button type="button" className="button button-secondary inline-button" onClick={() => setPeriods((current) => [...current, defaultPeriod()])}><Plus aria-hidden="true" /> Add rate period</button>
      <label className="workflow-confirm"><input name="officialAttestation" type="checkbox" required /> I attest these reusable rate facts and effective dates were transcribed from the recorded official SCE source.</label>
      {create.isError && <Notice kind="warning"><strong>Manual candidate was not created.</strong> {create.error instanceof Error ? create.error.message : 'The server rejected the candidate.'}</Notice>}
      <div className="dialog-actions"><button type="button" className="button button-secondary" onClick={close} disabled={create.isPending}>Cancel</button><button type="submit" className="button button-primary" disabled={create.isPending}>{create.isPending ? 'Validating complete schedule…' : 'Create review-required candidate'}</button></div>
    </form>
  </Dialog>;
}
