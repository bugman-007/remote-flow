import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { KeyRound, ShieldOff, UserCheck, UserX } from "lucide-react";
import { api, errorMessage } from "../../lib/api";
import { Badge, Button, Card, ErrorNote, Textarea } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import { t } from "../../i18n";
import { formatDateTime } from "../../lib/format";
import type { User } from "../../types";

function Stat({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <div>
      <dt className="rf-label">{label}</dt>
      <dd className="text-sm font-medium">
        {value}
        {hint ? <span className="ml-1 text-xs font-normal text-muted-foreground">{hint}</span> : null}
      </dd>
    </div>
  );
}

/**
 * USR-9: one person as a card — the Manager sees the numbers that matter for
 * that role, can keep a free-form note, and gets the account actions on the footer.
 */
export function UserCard({
  user,
  onEdit,
  onResetPassword,
  onRevokeSessions,
  onToggleActive,
}: {
  user: User;
  onEdit: () => void;
  onResetPassword: () => void;
  onRevokeSessions: () => void;
  onToggleActive: () => void;
}) {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [info, setInfo] = useState(user.info ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const stats = user.stats ?? null;

  useEffect(() => {
    setInfo(user.info ?? "");
  }, [user.info]);

  const saveInfo = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.patch(`/users/${user.id}`, { info });
      push({ tone: "success", title: t("users.infoSaved") });
      await queryClient.invalidateQueries({ queryKey: ["users"] });
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const dirty = info !== (user.info ?? "");

  return (
    <Card className="flex h-full flex-col">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate font-medium">{user.name}</p>
          <p className="truncate text-xs text-muted-foreground">{user.email}</p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Badge tone={user.is_active ? "success" : "neutral"}>
            {user.is_active ? t("users.status.active") : t("users.status.deactivated")}
          </Badge>
          <Badge tone="outline">{user.role}</Badge>
        </div>
      </div>

      {user.role === "maker" ? (
        <dl className="mt-3 grid grid-cols-2 gap-2">
          <Stat label={t("users.stats.totalResumes")} value={stats?.total_resumes ?? 0} />
          <Stat label={t("users.stats.todayResumes")} value={stats?.resumes_today ?? 0} />
          <Stat
            label={t("users.stats.interviews")}
            value={stats?.interviews ?? 0}
            hint={t("users.stats.interviewRate", { pct: stats?.interview_pct ?? 0 })}
          />
          <Stat label={t("users.profile")} value={user.profile_name ?? "—"} />
          <Stat
            label={t("users.dailyLimit")}
            value={user.daily_limit ?? "—"}
            hint={`${t("users.usedToday")}: ${user.usage_today ?? 0}`}
          />
          <Stat label={t("users.lastLogin")} value={user.last_login_at ? formatDateTime(user.last_login_at) : "—"} />
        </dl>
      ) : null}

      {user.role === "reviewer" ? (
        <dl className="mt-3 grid grid-cols-2 gap-2">
          <Stat label={t("users.stats.reviewsToday")} value={stats?.reviews_today ?? 0} />
          <Stat label={t("users.stats.totalInterviews")} value={stats?.total_interviews ?? 0} />
          <div className="col-span-2">
            <dt className="rf-label">{t("users.stats.byStep")}</dt>
            <dd className="mt-1 flex flex-wrap gap-1">
              {(stats?.by_step ?? []).length === 0 ? (
                <span className="text-xs text-muted-foreground">{t("users.stats.noSteps")}</span>
              ) : (
                (stats?.by_step ?? []).map((entry) => (
                  <span
                    key={entry.name}
                    className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs"
                    style={
                      entry.color
                        ? { borderColor: entry.color, color: entry.color, backgroundColor: `${entry.color}1a` }
                        : undefined
                    }
                  >
                    {entry.name} · {entry.count}
                  </span>
                ))
              )}
            </dd>
          </div>
          <div className="col-span-2">
            <Stat label={t("users.lastLogin")} value={user.last_login_at ? formatDateTime(user.last_login_at) : "—"} />
          </div>
        </dl>
      ) : null}

      {user.role === "manager" ? (
        <dl className="mt-3 grid grid-cols-2 gap-2">
          <Stat label={t("users.lastLogin")} value={user.last_login_at ? formatDateTime(user.last_login_at) : "—"} />
          <Stat label={t("common.created")} value={formatDateTime(user.created_at)} />
        </dl>
      ) : null}

      <div className="mt-3">
        <p className="rf-label">{t("users.information")}</p>
        <Textarea
          className="mt-1 text-sm"
          placeholder={t("users.informationHint")}
          value={info}
          onChange={(event) => setInfo(event.target.value)}
        />
        {dirty ? (
          <Button className="mt-2" size="sm" loading={busy} onClick={() => void saveInfo()}>
            {t("users.saveInfo")}
          </Button>
        ) : null}
        {error ? (
          <div className="mt-2">
            <ErrorNote>{error}</ErrorNote>
          </div>
        ) : null}
      </div>

      <div className="mt-auto flex flex-wrap items-center gap-1 border-t border-border pt-2 tablet:mt-3">
        <Button size="sm" variant="ghost" onClick={onEdit}>
          {t("common.edit")}
        </Button>
        <Button size="sm" variant="ghost" title={t("users.resetPassword")} onClick={onResetPassword}>
          <KeyRound className="h-3.5 w-3.5" />
          {t("users.resetPassword")}
        </Button>
        <Button size="sm" variant="ghost" title={t("users.revokeSessions")} onClick={onRevokeSessions}>
          <ShieldOff className="h-3.5 w-3.5" />
        </Button>
        <Button size="sm" variant="ghost" title={user.is_active ? t("users.deactivate") : t("users.reactivate")} onClick={onToggleActive}>
          {user.is_active ? <UserX className="h-3.5 w-3.5" /> : <UserCheck className="h-3.5 w-3.5" />}
          {user.is_active ? t("users.deactivate") : t("users.reactivate")}
        </Button>
        <span className="ml-auto text-xs text-muted-foreground">{t("users.sessions", { count: user.sessions ?? 0 })}</span>
      </div>
    </Card>
  );
}
