import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, Bell, CreditCard, History as HistoryIcon, Home, LogOut, Search, Settings } from 'lucide-react';
import { useEffect, useState } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { api } from '../api';
import { useSession } from '../auth/SessionContext';
import { AlertDrawer } from '../components/AlertDrawer';
import { timeAgo } from '../lib/format';
import { useLiveUpdates } from '../hooks/useLiveUpdates';

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
  const home = useQuery({ queryKey: ['home'], queryFn: api.home, refetchInterval: 30_000 });
  const alerts = useQuery({ queryKey: ['alerts'], queryFn: api.alerts, refetchInterval: 30_000 });
  useLiveUpdates();

  useEffect(() => {
    const openAlerts = () => setAlertsOpen(true);
    window.addEventListener('pm:open-alerts', openAlerts);
    return () => window.removeEventListener('pm:open-alerts', openAlerts);
  }, []);

  const primary = home.data?.devices[0];
  const overallState = home.data?.devices.some((device) => ['offline', 'invalid', 'needs_attention'].includes(device.state)) ? 'degraded' : home.data ? 'healthy' : 'waiting';

  async function logout() {
    await api.logout();
    queryClient.setQueryData(['session'], { authenticated: false, bootstrap_required: false, user: null });
  }

  return <div className="app-shell">
    <a className="skip-link" href="#main-content">Skip to content</a>
    <aside className="sidebar">
      <div className="wordmark"><Activity aria-hidden="true" /><span>PowerMeter <strong>V2</strong></span></div>
      <nav aria-label="Primary navigation">{navigation.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to} end={to === '/'}><Icon aria-hidden="true" /><span>{label}</span></NavLink>)}</nav>
      <div className="sidebar-health">
        <div><span className={`health-dot health-${overallState}`} aria-hidden="true" /><strong>{overallState === 'healthy' ? 'All systems normal' : overallState === 'degraded' ? 'Attention needed' : 'Checking systems'}</strong></div>
        <small>{primary?.heartbeat_at ? `Updated ${timeAgo(primary.heartbeat_at)}` : 'No heartbeat received'}</small>
      </div>
    </aside>
    <div className="app-main">
      <header className="topbar">
        <div className="system-search"><span className={`health-dot health-${overallState}`} aria-hidden="true" /><label htmlFor="global-search" className="sr-only">Search settings and sensors</label><input id="global-search" type="search" placeholder="System status or search" /><Search aria-hidden="true" /></div>
        <div className="topbar-actions">
          <button type="button" className="notification-button" aria-label={`${alerts.data?.active_count ?? 0} active alerts`} onClick={() => setAlertsOpen(true)}><Bell aria-hidden="true" />{Boolean(alerts.data?.active_count) && <span>{alerts.data?.active_count}</span>}</button>
          <div className="profile"><span className="avatar" aria-hidden="true">{session.user?.display_name.slice(0, 1).toUpperCase()}</span><div><strong>{session.user?.display_name}</strong><small>{session.user?.roles[0] ?? 'Member'}</small></div></div>
          <button type="button" className="icon-button" onClick={() => void logout()} aria-label="Sign out"><LogOut aria-hidden="true" /></button>
        </div>
      </header>
      <main id="main-content" tabIndex={-1}><Outlet /></main>
    </div>
    <nav className="mobile-nav" aria-label="Primary navigation">{navigation.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to} end={to === '/'}><Icon aria-hidden="true" /><span>{label}</span></NavLink>)}</nav>
    <AlertDrawer open={alertsOpen} onClose={() => setAlertsOpen(false)} />
  </div>;
}
