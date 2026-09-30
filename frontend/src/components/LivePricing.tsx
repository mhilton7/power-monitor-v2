import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { api } from '../api';
import { isForbidden } from '../api/client';
import { useSession } from '../auth/SessionContext';
import { dateTime, money, numeric, percent } from '../lib/format';
import { useHeartbeatTickerNow } from '../lib/heartbeatTicker';

const unitPrice = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 6 });
function price(value: string | number | null | undefined) {
  return value === null || value === undefined ? 'Not available' : `${unitPrice.format(Number(value))}/kWh`;
}

/** This clock refreshes only the small pricing response, never History or billing. */
export function LivePricing({ homeId }: { homeId: string }) {
  const { can } = useSession();
  const permitted = can('rates.view') && can('billing.view');
  const query = useQuery({
    queryKey: ['live-pricing', homeId],
    queryFn: ({ signal }) => api.livePricing(homeId, signal),
    enabled: Boolean(homeId) && permitted,
    refetchInterval: 15_000,
    refetchOnWindowFocus: 'always',
    refetchOnReconnect: 'always',
    retry: false,
  });
  const [observedAt, setObservedAt] = useState(0);
  const browserNow = useHeartbeatTickerNow();
  const rate = query.data?.current_rate;
  const generatedAt = query.data?.generated_at;
  const deadline = query.data?.next_pricing_refresh_at;
  const freshUntil = rate?.load_fresh_until;
  const { refetch, dataUpdatedAt } = query;
  useEffect(() => {
    if (!generatedAt) return;
    // Server-relative deadlines tolerate a browser with the wrong timezone/clock.
    const serverTime = Date.parse(generatedAt);
    const elapsed = Math.max(0, Date.now() - dataUpdatedAt);
    const deadlines = [deadline, freshUntil].filter((value): value is string => Boolean(value))
      .map((value) => Date.parse(value) - serverTime - elapsed)
      .filter((delay) => Number.isFinite(delay) && delay > 0);
    if (deadlines.length === 0) return;
    const timer = window.setTimeout(() => {
      setObservedAt(Date.now());
      if (document.visibilityState !== 'hidden') void refetch({ cancelRefetch: false });
    }, Math.min(2_147_483_647, Math.min(...deadlines) + 25));
    return () => window.clearTimeout(timer);
  }, [dataUpdatedAt, deadline, freshUntil, generatedAt, observedAt, refetch]);

  const serverNow = generatedAt ? Date.parse(generatedAt) + Math.max(0, Math.max(browserNow, observedAt) - dataUpdatedAt) : 0;
  const priceExpired = Boolean(deadline && Date.parse(deadline) <= serverNow);
  const loadExpired = Boolean(freshUntil && Date.parse(freshUntil) <= serverNow);
  const timezone = rate?.timezone ?? query.data?.timezone ?? 'UTC';
  const state = query.data?.current_rate_state;
  const denied = !permitted || isForbidden(query.error) || state === 'permission_denied';
  const status = denied ? 'Permission required to view rates and billing.'
    : query.isPending ? 'Loading current electricity pricing…'
      : query.isError ? 'Current pricing could not be refreshed. Previously received values are not live.'
        : state === 'unconfigured' ? deadline ? 'Scheduled rate pending activation.' : 'No active electricity rate assignment.'
          : state === 'incomplete_usage' ? 'Account usage is incomplete; the current usage tier and price cannot be confirmed.'
            : !rate || state === 'unavailable' ? 'The current electricity price is unavailable.'
              : priceExpired ? 'Updating the scheduled electricity price…' : null;
  const currentPrice = state === 'available' && !denied && !query.isError && !priceExpired ? rate?.price_per_kwh : null;
  const liveCost = currentPrice !== null && currentPrice !== undefined && rate?.load_state === 'live' && !loadExpired
    ? rate.estimated_cost_per_hour : null;

  return <section className="dashboard-live-pricing" aria-label="Current electricity pricing" data-version-id={denied ? undefined : rate?.version_id} data-account-id={denied ? undefined : rate?.account_id}>
    <div className="dashboard-pricing-heading"><strong>Current electricity pricing</strong><small role="status" style={{ visibility: query.isFetching && !query.isPending ? 'visible' : 'hidden' }}>Refreshing</small></div>
    {status && <p role="status">{status}{query.isError && <button className="text-button" type="button" onClick={() => void refetch()}>Retry pricing</button>}</p>}
    {!denied && rate && <>
      <div className="dashboard-pricing-values">
        <div><span>{rate.plan_name}</span><strong>{price(currentPrice)}</strong><small>{[rate.tou_period ?? (rate.current_tier ? null : rate.period), rate.current_tier ? `Tier ${rate.current_tier}` : null].filter(Boolean).join(' · ')}</small></div>
        <div><span>Estimated cost at present load</span><strong>{liveCost === null || liveCost === undefined ? 'Not available' : `${money(liveCost)}/hour`}</strong><small>{rate.load_state === 'live' && !loadExpired ? 'Fresh authenticated load' : rate.load_state === 'stale' || loadExpired ? 'Stale load — live estimate unavailable' : 'No live load available'}{rate.load_measured_at ? ` · ${dateTime(rate.load_measured_at, timezone)}` : ''}</small></div>
      </div>
      {rate.current_tier !== null && rate.current_tier !== undefined && <p>Cycle usage: {numeric(rate.cycle_usage_kwh === null || rate.cycle_usage_kwh === undefined ? null : Number(rate.cycle_usage_kwh), 'kWh')}{rate.remaining_tier_kwh !== null && rate.remaining_tier_kwh !== undefined ? ` · ${numeric(Number(rate.remaining_tier_kwh), 'kWh')} before the next tier` : ' · No higher usage tier'}{rate.tier_progress_percent !== null && rate.tier_progress_percent !== undefined ? ` · ${percent(Number(rate.tier_progress_percent), true)} of tier threshold` : ''}</p>}
      <p>{rate.next_change_at ? <>Next scheduled change: {dateTime(rate.next_change_at, timezone)}{rate.next_period ? ` · ${rate.next_period}` : ''}{rate.next_price_per_kwh !== null && rate.next_price_per_kwh !== undefined ? ` · ${price(rate.next_price_per_kwh)}` : ' · Next price not yet determinable'}</> : 'No scheduled price change is currently determinable.'}</p>
      {rate.base_price_per_kwh !== null && rate.base_price_per_kwh !== undefined && <p>Base energy: {price(rate.base_price_per_kwh)}{rate.cca_adjustment_per_kwh !== null && rate.cca_adjustment_per_kwh !== undefined && Number(rate.cca_adjustment_per_kwh) !== 0 ? ` · CCA adjustment: ${price(rate.cca_adjustment_per_kwh)}` : ''}{rate.surcharge_percent !== null && rate.surcharge_percent !== undefined && Number(rate.surcharge_percent) !== 0 ? ` · Energy surcharge: ${percent(Number(rate.surcharge_percent), true)}` : ''}{rate.applied_baseline_credit_per_kwh !== null && rate.applied_baseline_credit_per_kwh !== undefined && Number(rate.applied_baseline_credit_per_kwh) !== 0 ? ` · Baseline credit: ${price(rate.applied_baseline_credit_per_kwh)}` : ''}</p>}
      <small>Marginal energy estimate; fixed charges and taxes are separate. Cost/hour is an estimate at this load, not an hourly total or a predicted bill. Usage-tier changes depend on consumption.</small>
    </>}
    {!rate && !denied && deadline && <p>Next rate evaluation: {dateTime(deadline, timezone)}</p>}
  </section>;
}
