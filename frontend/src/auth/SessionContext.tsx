/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, useMemo, type ReactNode } from 'react';
import type { Session } from '../api/schemas';

interface SessionContextValue {
  session: Session;
  can: (permission: string) => boolean;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({ session, children }: { session: Session; children: ReactNode }) {
  const value = useMemo<SessionContextValue>(() => ({
    session,
    can: (permission) => session.user?.permissions.includes(permission) ?? false,
  }), [session]);
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error('useSession must be used inside SessionProvider');
  return value;
}
