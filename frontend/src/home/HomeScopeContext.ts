import { createContext } from 'react';
import type { HomeScope } from '../api/schemas';

export interface HomeScopeContextValue {
  homeScopes: HomeScope[];
  selectedHomeId: string;
  selectedHome: HomeScope | undefined;
  setSelectedHomeId: (homeId: string) => void;
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  refetch: () => void;
}

export const HomeScopeContext = createContext<HomeScopeContextValue | null>(null);
