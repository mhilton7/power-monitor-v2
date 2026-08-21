import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, Bell, CreditCard, History as HistoryIcon, Home, LogOut, Settings } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { api } from '../api';
import { useSession } from '../auth/SessionContext';
import { AlertDrawer } from '../components/AlertDrawer';
import { HeartbeatAge } from '../components/HeartbeatAge';
import { useLiveUpdates } from '../hooks/useLiveUpdates';
import { useHomeScope } from '../home/useHomeScope';

const navigation = [
  { to: '/', label: 'Home', icon: Home },
  { to: '/history', label: 'History', icon: HistoryIcon },
  { to: '/billing', label: 'Billing', icon: CreditCard },
  { to: '/settings', label: 'Settings', icon: Settings },
] as const;

export function AppShell() {
  const [alertsOpen, setAlertsOpen] = useState(false);
  const { session } = useSession();
  const queryClient = useQueryClient();
  const location = useLocation();
  const priorPath = useRef(location.pathname);
  const { homeScopes, selectedHomeId, setSelectedHomeId, isLoading: homeScopesLoading, isError: homeScopesError } = useHomeScope();
  const preferences = useQuery({ queryKey: ['preferences'], queryFn: api.preferences });
  const refreshInterval = (preferences.data?.refresh_seconds ?? 30) * 1000;
  const home = useQuery({ queryKey: ['home', selectedHomeId], queryFn: () => api.home(selectedHomeId), enabled: Boolean(selectedHomeId), refetchInterval: refreshInterval });
  const alerts = useQuery({ queryKey: ['alerts'], queryFn: api.alerts, refetchInterval: refreshInterval });
  useLiveUpdates();

  useEffect(() => {
    const openAlerts = () => setAlertsOpen(true);
    window.addEventListener('pm:open-alerts', openAlerts);
    return () => window.removeEventListener('pm:open-alerts', openAlerts);
  }, []);

  useEffect(() => {
    if (!preferences.data) return;
    document.documentElement.dataset.density = preferences.data.density;
    document.documentElement.dataset.dateFormat = preferences.data.date_format;
    document.documentElement.dataset.timeFormat = preferences.data.time_format;
    document.documentElement.dataset.decimalPrecision = String(preferences.data.decimal_precision);
    document.documentElement.dataset.powerUnit = preferences.data.power_unit;
    document.documentElement.dataset.energyUnit = preferences.data.energy_unit;
    if (preferences.data.display_timezone) document.documentElement.dataset.displayTimezone = preferences.data.display_timezone;
    else delete document.documentElement.dataset.displayTimezone;
  }, [preferences.data]);

  useEffect(() => {
    const title = navigation.find((entry) => entry.to === location.pathname)?.label ?? 'PowerMeter V2';
    document.title = title === 'Home' ? 'PowerMeter V2' : `${title} · PowerMeter V2`;
    if (priorPath.current !== location.pathname) document.getElementById('main-content')?.focus();
    priorPath.current = location.pathname;
  }, [location.pathname]);

  const primary = home.data?.devices[0];
  const overallState = home.data?.devices.some((device) => ['offline', 'invalid', 'needs_attention'].includes(device.state)) ? 'degraded' : home.data ? 'healthy' : 'waiting';
  const homeNameCounts = new Map<string, number>();
  for (const homeScope of homeScopes) homeNameCounts.set(homeScope.name, (homeNameCounts.get(homeScope.name) ?? 0) + 1);
  const homeNameOrdinals = new Map<string, number>();
  const selectedHomeName = homeScopes.find((scope) => scope.id === selectedHomeId)?.name;

  async function logout() {
    await api.logout();
    queryClient.removeQueries({ predicate: (entry) => entry.queryKey[0] !== 'session' });
    queryClient.setQueryData(['session'], { authenticated: false, bootstrap_required: false, user: null });
  }

  return <div className="app-shell">
    <a className="skip-link" href="#main-content">Skip to content</a>
    <aside className="sidebar">
      <div className="wordmark"><Activity aria-hidden="true" /><span>PowerMeter <strong>V2</strong></span></div>
      <nav aria-label="Primary navigation">{navigation.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to} end={to === '/'} aria-label={label}><Icon aria-hidden="true" /><span>{label}</span></NavLink>)}</nav>
      <div className="sidebar-health">
        <div><span className={`health-dot health-${overallState}`} aria-hidden="true" /><strong>{overallState === 'healthy' ? 'All systems normal' : overallState === 'degraded' ? 'Attention needed' : 'Checking systems'}</strong></div>
        <small>{primary?.heartbeat_at ? <>Updated <HeartbeatAge timestamp={primary.heartbeat_at} /></> : 'No heartbeat received'}</small>
      </div>
    </aside>
    <div className="app-main">
      <header className="topbar">
        <div className="system-search home-switcher" title={selectedHomeName}><span className={`health-dot health-${overallState}`} aria-hidden="true" /><label htmlFor="active-home">Active home</label><select id="active-home" value={selectedHomeId} onChange={(event) => setSelectedHomeId(event.target.value)} disabled={homeScopesLoading || homeScopesError || homeScopes.length === 0} aria-describedby="active-home-status" title={selectedHomeName}>
          {homeScopesLoading && <option value="">Loading homes…</option>}
          {homeScopesError && <option value="">Homes unavailable</option>}
          {!homeScopesLoading && !homeScopesError && homeScopes.length === 0 && <option value="">No authorized homes</option>}
          {!homeScopesLoading && !homeScopesError && homeScopes.length > 1 && <option value="">Select an active home</option>}
          {homeScopes.map((homeScope) => {
            const ordinal = (homeNameOrdinals.get(homeScope.name) ?? 0) + 1;
            homeNameOrdinals.set(homeScope.name, ordinal);
            const label = homeNameCounts.get(homeScope.name) === 1 ? homeScope.name : `${homeScope.name} (${ordinal})`;
            return <option key={homeScope.id} value={homeScope.id}>{label}</option>;
          })}
        </select><span id="active-home-status" className="sr-only">{homeScopes.length > 1 && !selectedHomeId ? 'Choose a home before loading home-specific data.' : selectedHomeId ? 'Requests are scoped to the selected home.' : 'No home-specific requests are being made.'}</span></div>
        <div className="topbar-actions">
          <button type="button" className="notification-button" aria-label={`${alerts.data?.active_count ?? 0} active alerts across all authorized homes`} onClick={() => setAlertsOpen(true)}><Bell aria-hidden="true" />{Boolean(alerts.data?.active_count) && <span>{alerts.data?.active_count}</span>}</button>
          <div className="profile"><span className="avatar" aria-hidden="true">{session.user?.display_name.slice(0, 1).toUpperCase()}</span><div><strong>{session.user?.display_name}</strong><small>{session.user?.roles[0] ?? 'Member'}</small></div></div>
          <button type="button" className="icon-button" onClick={() => void logout()} aria-label="Sign out"><LogOut aria-hidden="true" /></button>
        </div>
      </header>
      <main id="main-content" tabIndex={-1}><Outlet key={selectedHomeId || 'no-home'} /></main>
    </div>
    <nav className="mobile-nav" aria-label="Primary navigation">{navigation.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to} end={to === '/'} aria-label={label}><Icon aria-hidden="true" /><span>{label}</span></NavLink>)}</nav>
    <AlertDrawer open={alertsOpen} onClose={() => setAlertsOpen(false)} />
  </div>;
}
