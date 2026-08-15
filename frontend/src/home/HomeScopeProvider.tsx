import { useQuery } from '@tanstack/react-query';
import { useCallback, useMemo, useState, type ReactNode } from 'react';
import { api } from '../api';
import type { HomeScope } from '../api/schemas';
import { HomeScopeContext, type HomeScopeContextValue } from './HomeScopeContext';

const noHomeScopes: HomeScope[] = [];

export function HomeScopeProvider({ children }: { children: ReactNode }) {
  const query = useQuery({ queryKey: ['home-scopes'], queryFn: api.homeScopes, staleTime: 30_000 });
  const { data, error, isError, isLoading, refetch: refetchQuery } = query;
  const [selection, setSelection] = useState({ homeId: '', scopeKey: '' });
  const homeScopes = data?.home_scopes ?? noHomeScopes;
  const scopeKey = homeScopes.map((home) => home.id).join('\n');

  const selectedHomeId = homeScopes.length === 1
    ? homeScopes[0]!.id
    : selection.scopeKey === scopeKey && homeScopes.some((home) => home.id === selection.homeId) ? selection.homeId : '';
  const setSelectedHomeId = useCallback((homeId: string) => {
    setSelection({ homeId, scopeKey });
  }, [scopeKey]);
  const refetch = useCallback(() => {
    void refetchQuery();
  }, [refetchQuery]);
  const value = useMemo<HomeScopeContextValue>(() => ({
    homeScopes,
    selectedHomeId,
    selectedHome: homeScopes.find((home) => home.id === selectedHomeId),
    setSelectedHomeId,
    isLoading,
    isError,
    error,
    refetch,
  }), [error, homeScopes, isError, isLoading, refetch, selectedHomeId, setSelectedHomeId]);

  return <HomeScopeContext.Provider value={value}>{children}</HomeScopeContext.Provider>;
}
