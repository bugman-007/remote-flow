import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  FileText,
  FormInput,
  LogOut,
  Menu,
  Moon,
  Settings,
  Sun,
  UserCog,
  Users,
  X,
} from "lucide-react";
import { useAuth } from "../auth/AuthProvider";
import { useRealtime } from "../realtime/RealtimeProvider";
import { useThemeMode } from "../theme/ThemeProvider";
import { t } from "../i18n";
import { cn } from "../lib/utils";
import { Badge, Button } from "../ui/primitives";
import { ConnectionIndicator } from "../components/ConnectionIndicator";
import { ChangePasswordDialog } from "../pages/ChangePasswordDialog";
import type { Role } from "../types";
import { initials } from "../lib/format";

interface NavItem {
  to: string;
  label: string;
  icon: typeof FileText;
}

const NAV: Record<Role, NavItem[]> = {
  maker: [
    { to: "/jd-upload", label: t("nav.jdUpload"), icon: FormInput },
    { to: "/resumes", label: t("nav.resumes"), icon: FileText },
  ],
  manager: [
    { to: "/resumes", label: t("nav.resumes"), icon: FileText },
    { to: "/interviews", label: t("nav.interviews"), icon: Users },
    { to: "/profiles", label: t("nav.profiles"), icon: UserCog },
    { to: "/settings", label: t("nav.settings"), icon: Settings },
    { to: "/users", label: t("nav.users"), icon: Users },
  ],
  reviewer: [{ to: "/interviews", label: t("nav.interviews"), icon: Users }],
};

export function AppShell() {
  const { user, logout } = useAuth();
  const { lastEventAt } = useRealtime();
  const { mode, resolved, setMode } = useThemeMode();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [unseen, setUnseen] = useState(0);
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    setMobileOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!user || user.role !== "reviewer") return;
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<{ count: number }>).detail;
      setUnseen(detail?.count ?? 0);
    };
    window.addEventListener("rf:unseen-interviews", handler);
    return () => window.removeEventListener("rf:unseen-interviews", handler);
  }, [user]);

  if (!user) return null;
  const items = NAV[user.role];

  const cycleTheme = () => {
    setMode(mode === "dark" ? "light" : mode === "light" ? "system" : "dark");
  };

  return (
    <div className="flex min-h-full bg-background">
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-40 flex w-60 shrink-0 flex-col border-r border-border bg-card transition-transform tablet:static tablet:translate-x-0",
          mobileOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex items-center justify-between gap-2 px-4 py-4">
          <div>
            <p className="text-sm font-semibold">{t("app.name")}</p>
            <p className="text-[11px] text-muted-foreground">{t("app.tagline")}</p>
          </div>
          <Button variant="ghost" size="icon" className="tablet:hidden" onClick={() => setMobileOpen(false)} aria-label={t("common.close")}>
            <X className="h-4 w-4" />
          </Button>
        </div>
        <nav className="flex-1 space-y-0.5 px-2">
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition",
                  isActive ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                )
              }
            >
              <item.icon className="h-4 w-4" />
              {item.label}
              {item.to === "/interviews" && user.role === "reviewer" && unseen > 0 ? (
                <Badge tone="info" className="ml-auto">
                  {unseen}
                </Badge>
              ) : null}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-border px-3 py-3">
          <div className="mb-2 flex items-center gap-2">
            <span className="flex h-8 w-8 items-center justify-center rounded-full bg-primary/10 text-xs font-semibold text-primary">
              {initials(user.name)}
            </span>
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">{user.name}</p>
              <p className="truncate text-[11px] text-muted-foreground">{user.role}</p>
            </div>
          </div>
          <div className="flex items-center justify-between">
            <ConnectionIndicator />
            <div className="flex items-center gap-1">
              <Button variant="ghost" size="icon" onClick={cycleTheme} title={`${t("theme.toggle")} (${mode})`}>
                {resolved === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
              </Button>
              <Button variant="ghost" size="icon" title={t("nav.logout")} onClick={() => void logout().then(() => navigate("/login"))}>
                <LogOut className="h-4 w-4" />
              </Button>
            </div>
          </div>
          <button
            type="button"
            className="mt-2 w-full rounded-md px-2 py-1 text-left text-xs text-muted-foreground hover:bg-accent"
            onClick={() => setPasswordOpen(true)}
          >
            {t("password.menuLabel")}
          </button>
          {lastEventAt ? (
            <p className="mt-1 px-2 text-[10px] text-muted-foreground">
              {t("connection.live")} · {new Date(lastEventAt).toLocaleTimeString()}
            </p>
          ) : null}
        </div>
      </aside>

      {mobileOpen ? <div className="fixed inset-0 z-30 bg-black/40 tablet:hidden" onClick={() => setMobileOpen(false)} /> : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-border bg-card px-4 py-2.5 tablet:hidden">
          <Button variant="ghost" size="icon" onClick={() => setMobileOpen(true)} aria-label="Menu">
            <Menu className="h-4 w-4" />
          </Button>
          <span className="text-sm font-semibold">{t("app.name")}</span>
          <span className="ml-auto">
            <ConnectionIndicator compact />
          </span>
        </header>
        <main className="min-w-0 flex-1 p-4 tablet:p-6">
          <Outlet />
        </main>
      </div>

      <ChangePasswordDialog
        open={passwordOpen}
        forced={false}
        onClose={() => setPasswordOpen(false)}
        onDone={() => setPasswordOpen(false)}
      />
    </div>
  );
}
