import { useQuery } from '@tanstack/react-query';
import { ArrowRight, CalendarDays, CheckCircle2, CircleAlert, DollarSign } from 'lucide-react';
import { useMemo, useState } from 'react';
import { api } from '../api';
import type { SceRateCatalogPlan } from '../api/schemas';
import { Card, Dialog, EmptyState, ErrorState, Loading, Notice, StatusPill } from '../components/ui';
import { dateTime } from '../lib/format';
import { sceRateCatalogKey } from './queryKeys';

type CatalogFilter = 'all' | 'tiered' | 'time_of_use' | 'ev' | 'discount' | 'existing' | 'current';

function words(value: string): string {
  return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function catalogSearchText(plan: SceRateCatalogPlan): string {
  return [
    plan.public_plan_name,
    plan.canonical_name,
    plan.official_schedule_code,
    plan.enrollment_status,
    ...plan.eligibility.map((entry) => typeof entry === 'string' ? entry : JSON.stringify(entry)),
  ].filter(Boolean).join(' ').toLocaleLowerCase();
}

function matchesFilter(plan: SceRateCatalogPlan, filter: CatalogFilter): boolean {
  const text = catalogSearchText(plan);
  if (filter === 'tiered') return plan.plan_type.includes('tiered');
  if (filter === 'time_of_use') return plan.plan_type.includes('time_of_use') || plan.plan_type === 'critical_peak_pricing' || plan.plan_type === 'dynamic_hourly';
  if (filter === 'ev') return /\b(ev|electric vehicle|electrification|prime)\b/i.test(text);
  if (filter === 'discount') return /\b(care|fera|medical baseline|discount)\b/i.test(text);
  if (filter === 'existing') return plan.enrollment_status === 'existing_customers_only';
  if (filter === 'current') return plan.currently_used;
  return true;
}

function exactAmount(value: string | number | null | undefined, suffix: string): string {
  return value === null || value === undefined ? 'Not reported' : `$${String(value)}${suffix ? ` ${suffix}` : ''}`;
}

function effectiveRange(plan: SceRateCatalogPlan): string {
  if (!plan.effective_start) return 'Effective date not reported';
  return `${plan.effective_start.slice(0, 10)}${plan.effective_end ? ` – ${plan.effective_end.slice(0, 10)}` : ' – current version'}`;
}

function planEligibility(plan: SceRateCatalogPlan): string {
  if (plan.eligibility_requirements.length > 0) return `${plan.eligibility_requirements.length} confirmation${plan.eligibility_requirements.length === 1 ? '' : 's'} required`;
  if (plan.eligibility.length > 0) return `${plan.eligibility.length} requirement${plan.eligibility.length === 1 ? '' : 's'}`;
  return 'No special requirement reported';
}

function planFixedCharge(plan: SceRateCatalogPlan): string {
  if (plan.daily_fixed_charge !== null && plan.daily_fixed_charge !== undefined) return exactAmount(plan.daily_fixed_charge, 'per day');
  if (plan.monthly_fixed_charge !== null && plan.monthly_fixed_charge !== undefined) return exactAmount(plan.monthly_fixed_charge, 'per month');
  if (plan.minimum_charge !== null && plan.minimum_charge !== undefined) return exactAmount(plan.minimum_charge, 'minimum');
  return 'Not reported';
}

function eligibilityLabel(entry: SceRateCatalogPlan['eligibility'][number]): string {
  if (typeof entry === 'string') return words(entry);
  for (const key of ['label', 'description', 'requirement', 'name']) {
    if (typeof entry[key] === 'string') return entry[key];
  }
  return 'See the official eligibility details';
}

function componentName(component: SceRateCatalogPlan['periods'][number]['rate_components'][number]): string {
  if (typeof component === 'string') return component;
  for (const key of ['component', 'name', 'label']) if (typeof component[key] === 'string') return component[key];
  return 'other charge';
}

function componentAmount(component: SceRateCatalogPlan['periods'][number]['rate_components'][number]): string | null {
  if (typeof component === 'string') return null;
  for (const key of ['amount_per_kwh', 'rate_per_kwh', 'amount', 'rate']) {
    if (typeof component[key] === 'string' || typeof component[key] === 'number') return String(component[key]);
  }
  return null;
}

function scheduleComponent(period: SceRateCatalogPlan['periods'][number], category: 'delivery' | 'generation' | 'credit' | 'other'): string {
  const selected = period.rate_components.filter((component) => {
    const name = componentName(component).toLocaleLowerCase();
    const combined = name.includes('combined') || (name.includes('delivery') && name.includes('generation'));
    if (category === 'other') return combined || (!name.includes('delivery') && !name.includes('generation') && !name.includes('credit'));
    if (combined) return false;
    return name.includes(category);
  });
  if (selected.length === 0) return '—';
  return selected.map((component) => {
    const amount = componentAmount(component);
    return amount === null ? words(componentName(component)) : `$${amount}`;
  }).join(' + ');
}

function scheduleDayAndTime(plan: SceRateCatalogPlan, period: SceRateCatalogPlan['periods'][number]): { day: string; time: string } {
  const tiered = plan.plan_type.includes('tiered');
  if (tiered) {
    const name = period.period_name ? words(period.period_name) : 'Usage tier';
    const lower = period.tier.lower_bound_kwh;
    const time = lower !== null && lower !== undefined && Number(lower) > 0 ? 'Above baseline' : 'Through baseline';
    return { day: name, time };
  }
  return {
    day: period.day_type ? words(period.day_type) : 'All days',
    time: period.local_start_time && period.local_end_time ? `${period.local_start_time}–${period.local_end_time}` : 'Time not reported',
  };
}

function CatalogDetails({ plan }: { plan: SceRateCatalogPlan }) {
  const discoveryState = plan.latest_discovery_state ?? plan.verification_state;
  return <div className="catalog-details">
    {discoveryState === 'requires_parser' && <Notice kind="warning"><strong>Parser update needed.</strong> {plan.last_known_good_retained ? 'The last successfully parsed schedule remains visible for comparison, but this newer source revision is not ready to apply.' : 'This official plan remains listed, but its full schedule is not ready to apply.'}</Notice>}
    {discoveryState === 'excluded' && <Notice><strong>Explicitly excluded.</strong> {plan.exclusion_reason ?? 'No exclusion reason was returned.'}</Notice>}
    {!plan.exact_rates_verified && plan.periods.length > 0 && <Notice kind="warning"><strong>Rounded public prices.</strong> These consumer-page values are preserved as displayed evidence. They are not approved as exact calculation rates until an authoritative tariff document and effective date are reviewed.</Notice>}
    <section aria-labelledby="catalog-overview-heading"><h3 id="catalog-overview-heading">Overview</h3><dl>
      <div><dt>Official name</dt><dd>{plan.public_plan_name}</dd></div>
      <div><dt>Schedule code</dt><dd>{plan.official_schedule_code ?? 'Not reported'}</dd></div>
      <div><dt>Plan type</dt><dd>{words(plan.plan_type)}</dd></div>
      <div><dt>Enrollment</dt><dd>{words(plan.enrollment_status)}</dd></div>
      <div><dt>Effective period</dt><dd>{effectiveRange(plan)}</dd></div>
      <div><dt>Description</dt><dd>{plan.description ?? 'Not reported'}</dd></div>
      <div><dt>Current rate plan</dt><dd>{plan.currently_used ? 'Yes' : 'No'}</dd></div>
    </dl></section>
    <section aria-labelledby="catalog-eligibility-heading"><h3 id="catalog-eligibility-heading">Eligibility</h3>{plan.eligibility_requirements.length > 0 ? <ul>{plan.eligibility_requirements.map((entry, index) => <li key={`${eligibilityLabel(entry.requirement)}-${index}`}><strong>{eligibilityLabel(entry.requirement)}</strong><span>{words(entry.verification)}</span></li>)}</ul> : plan.eligibility.length > 0 ? <ul>{plan.eligibility.map((entry, index) => <li key={`${eligibilityLabel(entry)}-${index}`}>{eligibilityLabel(entry)}</li>)}</ul> : <p>No special eligibility requirement was reported.</p>}</section>
    <section aria-labelledby="catalog-charges-heading"><h3 id="catalog-charges-heading">Fixed charges and credits</h3><dl>
      <div><dt>Daily charge</dt><dd>{exactAmount(plan.daily_fixed_charge, 'per day')}</dd></div>
      <div><dt>Monthly charge</dt><dd>{exactAmount(plan.monthly_fixed_charge, 'per month')}</dd></div>
      <div><dt>Minimum charge</dt><dd>{exactAmount(plan.minimum_charge, '')}</dd></div>
      <div><dt>Meter charge</dt><dd>{exactAmount(plan.meter_charge, '')}</dd></div>
      <div><dt>Other fixed charge</dt><dd>{exactAmount(plan.other_fixed_charge, '')}</dd></div>
      <div><dt>Baseline credit</dt><dd>{exactAmount(plan.baseline_credit_per_kwh, 'per kWh')}</dd></div>
      <div><dt>Tier rule</dt><dd>{plan.tier_threshold_basis ? words(plan.tier_threshold_basis) : 'Not applicable or not reported'}</dd></div>
    </dl></section>
    <section aria-labelledby="catalog-schedule-heading"><h3 id="catalog-schedule-heading">Schedule overview</h3><dl>
      <div><dt>Seasons</dt><dd>{plan.seasons.length > 0 ? plan.seasons.map(words).join(', ') : 'Not reported'}</dd></div>
      <div><dt>Day types</dt><dd>{plan.day_types.length > 0 ? plan.day_types.map(words).join(', ') : 'Not reported'}</dd></div>
      <div><dt>Rate periods</dt><dd>{plan.period_count}</dd></div>
      <div><dt>Holiday treatment</dt><dd>{plan.holiday_treatment ? words(plan.holiday_treatment) : 'Not reported'}</dd></div>
      <div><dt>Price precision</dt><dd>{plan.exact_rates_verified ? 'Exact approved tariff values' : words(plan.rate_precision)}</dd></div>
      <div><dt>Latest source result</dt><dd>{words(discoveryState)}</dd></div>
      <div><dt>Saved schedule status</dt><dd>{plan.last_known_good_retained ? 'Last successfully parsed version retained' : words(plan.verification_state)}</dd></div>
    </dl>{plan.periods.length > 0 ? <div className="catalog-schedule-table-wrap"><table className="catalog-schedule-table"><caption className="sr-only">{plan.exact_rates_verified ? 'Exact' : 'Rounded public'} schedule for {plan.public_plan_name}</caption><thead><tr><th>Season</th><th>Day</th><th>Time</th><th>Rate period</th><th>Delivery</th><th>Generation</th><th>Other charges</th><th>Credit</th><th>{plan.exact_rates_verified ? 'Exact total' : 'Rounded public total'}</th></tr></thead><tbody>{plan.periods.map((period, index) => {
      const friendly = scheduleDayAndTime(plan, period);
      return <tr key={`${period.season}-${period.day_type}-${period.period_name}-${period.start_minute}-${index}`}><td data-label="Season">{period.season ? words(period.season) : 'All seasons'}</td><td data-label="Day">{friendly.day}</td><td data-label="Time">{friendly.time}</td><td data-label="Rate period">{period.period_name ? words(period.period_name) : 'Not reported'}</td><td data-label="Delivery">{scheduleComponent(period, 'delivery')}</td><td data-label="Generation">{scheduleComponent(period, 'generation')}</td><td data-label="Other charges">{scheduleComponent(period, 'other')}</td><td data-label="Credit">{scheduleComponent(period, 'credit')}</td><td data-label={plan.exact_rates_verified ? 'Exact total' : 'Rounded public total'}>{period.price_per_kwh === null ? 'Not reported' : `$${String(period.price_per_kwh)}/${period.energy_unit}`}</td></tr>;
    })}</tbody></table></div> : <p className="disclosure">Exact schedule rows are not available for this record. No missing rate is inferred in the browser.</p>}</section>
    <details className="technical-details"><summary>Technical details</summary><dl>
      <div><dt>Source</dt><dd><a href={plan.source.url} target="_blank" rel="noreferrer">{plan.source.name}</a></dd></div>
      <div><dt>Checked</dt><dd>{dateTime(plan.source.retrieved_at)}</dd></div>
      <div><dt>Parser</dt><dd><code>{plan.source.parser_version}</code></dd></div>
      <div><dt>Source revision</dt><dd><code>{plan.source.revision_id}</code></dd></div>
      <div><dt>Latest discovery revision</dt><dd><code>{plan.latest_discovery_revision_id}</code></dd></div>
      <div><dt>SHA-256</dt><dd><code>{plan.source.artifact_sha256}</code></dd></div>
      <div><dt>Catalog record</dt><dd><code>{plan.id}</code></dd></div>
    </dl></details>
  </div>;
}

export function SceRateCatalog({ homeId }: { homeId: string }) {
  const [filter, setFilter] = useState<CatalogFilter>('all');
  const [selectedId, setSelectedId] = useState('');
  const catalog = useQuery({ queryKey: sceRateCatalogKey(homeId), queryFn: () => api.sceRateCatalog(homeId), enabled: Boolean(homeId), refetchInterval: 60_000 });
  const plans = useMemo(() => catalog.data?.plans.filter((plan) => matchesFilter(plan, filter)) ?? [], [catalog.data?.plans, filter]);
  const selected = catalog.data?.plans.find((plan) => plan.id === selectedId);

  return <>
    <Card title="Available SCE rate plans" eyebrow="Official public SCE catalog" action={<div className="field compact-field"><label htmlFor="sce-catalog-filter">Filter plans</label><select id="sce-catalog-filter" value={filter} onChange={(event) => setFilter(event.target.value as CatalogFilter)}><option value="all">All plans</option><option value="tiered">Tiered</option><option value="time_of_use">Time of Use</option><option value="ev">EV &amp; electrification</option><option value="discount">Discount plans</option><option value="existing">Existing customers only</option><option value="current">Current plan</option></select></div>}>
      {catalog.isLoading ? <Loading label="Loading official SCE rate plans" /> : catalog.isError ? <ErrorState error={catalog.error} retry={() => void catalog.refetch()} /> : catalog.data ? <>
        <div className="catalog-summary-grid" aria-label="SCE catalog summary">
          <article><CheckCircle2 aria-hidden="true" /><span>Plans discovered</span><strong>{catalog.data.summary.plans_discovered}</strong></article>
          <article><CheckCircle2 aria-hidden="true" /><span>Plans parsed</span><strong>{catalog.data.summary.plans_parsed}</strong></article>
          <article><CircleAlert aria-hidden="true" /><span>Need parser update</span><strong>{catalog.data.summary.plans_requiring_parser_updates}</strong></article>
          <article><CircleAlert aria-hidden="true" /><span>Explicitly excluded</span><strong>{catalog.data.summary.plans_explicitly_excluded}</strong></article>
          <article><CheckCircle2 aria-hidden="true" /><span>Open plans</span><strong>{catalog.data.summary.open_plans}</strong></article>
          <article><CircleAlert aria-hidden="true" /><span>Eligibility required</span><strong>{catalog.data.summary.eligibility_required_plans}</strong></article>
          <article><CircleAlert aria-hidden="true" /><span>Existing customers only</span><strong>{catalog.data.summary.existing_customer_only_plans}</strong></article>
          <article><CalendarDays aria-hidden="true" /><span>Last successful check</span><strong>{catalog.data.summary.last_successful_official_check ? dateTime(catalog.data.summary.last_successful_official_check) : 'Not yet checked'}</strong></article>
          <article><DollarSign aria-hidden="true" /><span>Current catalog date</span><strong>{catalog.data.summary.current_catalog_effective_date?.slice(0, 10) ?? 'Not reported'}</strong></article>
        </div>
        {catalog.data.catalog_ready
          ? <Notice kind="info"><strong>Official catalog crawl accounted for every discovered in-scope document.</strong> No plan link was silently omitted. Exact rates, eligibility, and home-specific parameters still require the normal reviewed publication workflow.</Notice>
          : <Notice kind="warning"><strong>Catalog inventory is incomplete.</strong> The bounded official-source crawl has not yet accounted for every discovered in-scope document. Silently omitted plans: {catalog.data.summary.plans_silently_omitted === null ? 'unknown' : catalog.data.summary.plans_silently_omitted}.</Notice>}
        {plans.length > 0 ? <div className="catalog-plan-list">{plans.map((plan) => {
          const discoveryState = plan.latest_discovery_state ?? plan.verification_state;
          return <button type="button" key={plan.id} onClick={() => setSelectedId(plan.id)} aria-label={`View ${plan.public_plan_name} rate plan`}><div className="catalog-plan-copy"><strong>{plan.public_plan_name}</strong><span>{plan.official_schedule_code ?? 'Schedule code not reported'} · {words(plan.plan_type)}</span><small>{effectiveRange(plan)} · {plan.period_count} rate period{plan.period_count === 1 ? '' : 's'}</small><dl className="catalog-plan-summary"><div><dt>Enrollment</dt><dd>{words(plan.enrollment_status)}</dd></div><div><dt>Eligibility</dt><dd>{planEligibility(plan)}</dd></div><div><dt>Seasons</dt><dd>{plan.seasons.length > 0 ? plan.seasons.map(words).join(', ') : 'Not reported'}</dd></div><div><dt>Fixed charge</dt><dd>{planFixedCharge(plan)}</dd></div><div><dt>Tier or baseline rule</dt><dd>{plan.tier_threshold_basis ? words(plan.tier_threshold_basis) : 'Not applicable or not reported'}</dd></div><div><dt>Currently used</dt><dd>{plan.currently_used ? 'Yes' : 'No'}</dd></div></dl></div><StatusPill state={plan.currently_used && discoveryState === 'parsed' ? 'approved' : discoveryState === 'parsed' ? 'healthy' : discoveryState === 'requires_parser' ? 'warning' : 'neutral'} label={plan.currently_used && discoveryState === 'parsed' ? 'Current plan' : words(discoveryState)} /><ArrowRight aria-hidden="true" /></button>;
        })}</div> : <EmptyState title="No plans match this filter" detail="Choose another catalog filter. No rate data was changed." />}
        <details className="technical-details"><summary>Technical details</summary><dl><div><dt>Source policy</dt><dd>Official public SCE sources only</dd></div><div><dt>Inventory scope</dt><dd>{words(catalog.data.inventory_scope)}</dd></div><div><dt>Catalog completeness</dt><dd>{words(catalog.data.catalog_completeness)}</dd></div><div><dt>Completeness reason</dt><dd>{words(catalog.data.completeness_reason)}</dd></div><div><dt>Ready to apply as a complete catalog</dt><dd>{catalog.data.catalog_ready ? 'Yes' : 'No'}</dd></div><div><dt>Live source access in this response</dt><dd>{catalog.data.live_source_access_performed ? 'Yes' : 'No; last saved official check shown'}</dd></div></dl></details>
      </> : null}
    </Card>
    <Dialog open={Boolean(selectedId)} title={selected?.public_plan_name ?? 'SCE rate plan details'} description="Official public rate information. Eligibility and home-specific baseline requirements must still be confirmed before use." onClose={() => setSelectedId('')} wide>{selected ? <CatalogDetails plan={selected} /> : <ErrorState error={new Error('This catalog plan is no longer available.')} />}</Dialog>
  </>;
}
