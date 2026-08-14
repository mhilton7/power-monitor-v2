import type { ReactNode } from 'react';
import { useSession } from './SessionContext';

interface PermissionGateProps {
  permission: string;
  children: ReactNode;
  fallback?: ReactNode;
}

export function PermissionGate({ permission, children, fallback = null }: PermissionGateProps) {
  const { can } = useSession();
  return can(permission) ? children : fallback;
}
