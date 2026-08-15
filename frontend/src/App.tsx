import { useQuery, useQueryClient } from '@tanstack/react-query';
import { lazy, Suspense, useEffect, useState } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { api } from './api';
import type { Session } from './api/schemas';
import { AuthScreen } from './auth/AuthScreen';
import { SessionProvider } from './auth/SessionContext';
import { ErrorState, Loading, Notice } from './components/ui';
import { AppShell } from './layout/AppShell';
import { HomeScopeProvider } from './home/HomeScopeProvider';

const HomePage = lazy(async () => ({ default: (await import('./pages/HomePage')).HomePage }));
const HistoryPage = lazy(async () => ({ default: (await import('./pages/HistoryPage')).HistoryPage }));
const BillingPage = lazy(async () => ({ default: (await import('./pages/BillingPage')).BillingPage }));
const SettingsPage = lazy(async () => ({ default: (await import('./pages/SettingsPage')).SettingsPage }));

export function App() {
  const queryClient = useQueryClient();
  const [expired, setExpired] = useState(false);
  const query = useQuery({ queryKey: ['session'], queryFn: api.session, retry: false, staleTime: 30_000 });

  useEffect(() => {
    const onExpired = () => {
      setExpired(true);
      queryClient.setQueryData<Session>(['session'], { authenticated: false, bootstrap_required: false, user: null });
      queryClient.removeQueries({ predicate: (entry) => entry.queryKey[0] !== 'session' });
    };
    window.addEventListener('pm:session-expired', onExpired);
    return () => window.removeEventListener('pm:session-expired', onExpired);
  }, [queryClient]);

  if (query.isLoading) return <div className="full-page-center"><Loading label="Opening PowerMeter V2" /></div>;
  if (query.isError) return <div className="full-page-center"><ErrorState error={query.error} retry={() => void query.refetch()} /></div>;
  const session = query.data;
  if (!session) return <div className="full-page-center"><ErrorState error={new Error('The session response was empty.')} retry={() => void query.refetch()} /></div>;
  if (!session.authenticated) return <>{expired && <div className="session-banner"><Notice kind="warning">Your session expired. Sign in again to continue.</Notice></div>}<AuthScreen bootstrap={session.bootstrap_required} /></>;

  return <SessionProvider session={session}>
    <HomeScopeProvider>
      <Suspense fallback={<Loading label="Loading view" />}>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<HomePage />} />
            <Route path="history" element={<HistoryPage />} />
            <Route path="billing" element={<BillingPage />} />
            <Route path="settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </Suspense>
    </HomeScopeProvider>
  </SessionProvider>;
}
