import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { ROLE_HOME, useAuth } from "./AuthProvider";
import { Spinner } from "../ui/primitives";
import { t } from "../i18n";
import type { Role } from "../types";

function FullPageSpinner() {
  return (
    <div className="flex min-h-screen items-center justify-center gap-2 text-muted-foreground">
      <Spinner /> <span className="text-sm">{t("common.loading")}</span>
    </div>
  );
}

export function RequireAuth({ children, roles }: { children: ReactNode; roles?: Role[] }) {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <FullPageSpinner />;
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  if (user.must_change_password) return <Navigate to="/change-password" replace />;
  if (roles && !roles.includes(user.role)) return <Navigate to={ROLE_HOME[user.role]} replace />;
  return <>{children}</>;
}

export function RedirectHome() {
  const { user, loading } = useAuth();
  if (loading) return <FullPageSpinner />;
  if (!user) return <Navigate to="/login" replace />;
  return <Navigate to={user.must_change_password ? "/change-password" : ROLE_HOME[user.role]} replace />;
}
