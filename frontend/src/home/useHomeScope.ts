import { useContext } from 'react';
import { HomeScopeContext, type HomeScopeContextValue } from './HomeScopeContext';

export function useHomeScope(): HomeScopeContextValue {
  const value = useContext(HomeScopeContext);
  if (!value) throw new Error('useHomeScope must be used within HomeScopeProvider.');
  return value;
}
