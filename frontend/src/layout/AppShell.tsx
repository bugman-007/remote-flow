import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  ChevronDown,
  FileStack,
  FileText,
  FormInput,
  KeyRound,
  LogOut,
  Menu,
  Moon,
  Radio,
  Settings,
  Sparkles,
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
import { Button } from "../ui/primitives";
import { DropdownMenu, MenuItem, MenuSeparator } from "../ui/menu";
import { ConnectionIndicator } from "../components/ConnectionIndicator";
import { GlobalLoadingBar } from "../components/GlobalLoadingBar";
import { ChangePasswordDialog } from "../pages/ChangePasswordDialog";
import type { Role } from "../types";
import { formatTime, initials } from "../lib/format";

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
    { to: "/bulk-resumes", label: t("nav.bulkResumes"), icon: FileStack },
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

  const roleLabel = t(`roles.${user.role}`);

  return (
    <div className="flex min-h-full bg-background">
      <GlobalLoadingBar />
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-40 flex w-60 shrink-0 flex-col border-r border-border bg-surface transition-transform tablet:sticky tablet:top-0 tablet:h-screen tablet:translate-x-0",
          mobileOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex items-center justify-between gap-2 border-b border-border px-5 py-5">
          <div className="flex min-w-0 items-center gap-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-primary text-primary-foreground shadow-glow">
              <Sparkles className="h-4 w-4" />
            </span>
            <div className="min-w-0">
              <p className="truncate text-[0.95rem] font-semibold tracking-tight">{t("app.name")}</p>
              <p className="truncate text-[11px] text-muted-foreground">{t("app.shortTagline")}</p>
            </div>
          </div>
          <Button variant="ghost" size="icon" className="tablet:hidden" onClick={() => setMobileOpen(false)} aria-label={t("common.close")}>
            <X className="h-4 w-4" />
          </Button>
        </div>
        <nav className="flex-1 space-y-1 overflow-y-auto px-3 py-4">
          <p className="px-3 pb-2 text-[11px] font-medium uppercase tracking-[0.12em] text-muted-foreground/70">{t("nav.section")}</p>
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                cn(
                  "group relative flex h-10 items-center gap-3 rounded-md px-3 text-sm transition",
                  isActive
                    ? "border border-border bg-accent font-medium text-foreground shadow-card"
                    : "border border-transparent text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                )
              }
            >
              {({ isActive }) => (
                <>
                  <item.icon className={cn("h-4 w-4", isActive ? "text-primary" : "text-muted-foreground group-hover:text-foreground")} />
                  {item.label}
                  {item.to === "/interviews" && user.role === "reviewer" && unseen > 0 ? (
                    <span className="ml-auto rounded-full bg-primary/15 px-2 py-0.5 text-[11px] font-semibold text-primary">{unseen}</span>
                  ) : null}
                  {isActive ? <span className="absolute -right-3 top-2 h-6 w-1 rounded-l-full bg-primary/80" /> : null}
                </>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="p-3">
          <div className="rf-card flex items-center gap-3 px-3 py-3">
            <span className="flex h-8 w-8 items-center justify-center rounded-full bg-primary/15 text-primary">
              <Radio className="h-4 w-4" />
            </span>
            <div className="min-w-0">
              <ConnectionIndicator />
              <p className="truncate text-[11px] text-muted-foreground">
                {lastEventAt ? t("connection.lastEvent", { time: formatTime(lastEventAt) }) : t("connection.waiting")}
              </p>
            </div>
          </div>
        </div>
      </aside>

      {mobileOpen ? <div className="fixed inset-0 z-30 bg-black/60 backdrop-blur-sm tablet:hidden" onClick={() => setMobileOpen(false)} /> : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-16 items-center gap-3 border-b border-border bg-background/80 px-4 backdrop-blur-md tablet:px-8">
          <Button variant="outline" size="icon" className="tablet:hidden" onClick={() => setMobileOpen(true)} aria-label="Menu">
            <Menu className="h-4 w-4" />
          </Button>
          <span className="text-sm font-semibold tablet:hidden">{t("app.name")}</span>
          <div className="ml-auto flex items-center gap-2">
            <span className="hidden rounded-full border border-border bg-surface px-3 py-1.5 tablet:inline-flex">
              <ConnectionIndicator />
            </span>
            <Button
              variant="outline"
              size="icon"
              className="h-9 w-9 rounded-full"
              onClick={cycleTheme}
              title={`${t("theme.toggle")} (${mode})`}
              aria-label={t("theme.toggle")}
            >
              {resolved === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </Button>
            <DropdownMenu
              triggerClassName="h-11 rounded-full border border-border bg-surface pl-1.5 pr-3 hover:bg-accent"
              label={
                <span className="flex items-center gap-2.5">
                  <span className="flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-primary to-primary/40 text-xs font-semibold text-primary-foreground">
                    {initials(user.name)}
                  </span>
                  <span className="hidden min-w-0 text-left leading-tight tablet:block">
                    <span className="block text-[11px] text-muted-foreground">{user.email}</span>
                    <span className="flex items-center gap-1.5 text-sm font-medium">
                      {user.name}
                      <span className="rounded-full bg-primary/15 px-1.5 py-px text-[10px] font-semibold uppercase tracking-wide text-primary">
                        {roleLabel}
                      </span>
                    </span>
                  </span>
                  <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
                </span>
              }
            >
              {(close) => (
                <>
                  <MenuItem
                    onSelect={() => {
                      close();
                      setPasswordOpen(true);
                    }}
                  >
                    <KeyRound className="mr-2 inline h-3.5 w-3.5" />
                    {t("password.menuLabel")}
                  </MenuItem>
                  <MenuSeparator />
                  <MenuItem
                    onSelect={() => {
                      close();
                      void logout().then(() => navigate("/login"));
                    }}
                  >
                    <LogOut className="mr-2 inline h-3.5 w-3.5" />
                    {t("nav.logout")}
                  </MenuItem>
                </>
              )}
            </DropdownMenu>
          </div>
        </header>
        <main className="min-w-0 flex-1 px-4 py-6 tablet:px-6 tablet:py-8">
          <div className="mx-auto w-full max-w-[1600px]">
            <Outlet />
          </div>
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
