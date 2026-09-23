import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Plus, RefreshCw, Upload } from "lucide-react";
import { api, errorMessage } from "../lib/api";
import { t } from "../i18n";
import { Button, Card, Checkbox, EmptyState, ErrorNote, Field, Input, Select, Spinner } from "../ui/primitives";
import { Dialog } from "../ui/dialog";
import { useToast } from "../ui/toast";
import { UserCard } from "./users/UserCard";
import type { Profile, User } from "../types";

export function UsersPage() {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [filters, setFilters] = useState({ role: "", status: "", q: "" });
  const [editing, setEditing] = useState<User | null>(null);
  const [creating, setCreating] = useState(false);
  const [temporary, setTemporary] = useState<{ email: string; password: string }[] | null>(null);
  const [importOpen, setImportOpen] = useState(false);

  const users = useQuery({
    queryKey: ["users", filters],
    queryFn: () => api.get<{ items: User[] }>("/users", { role: filters.role || undefined, status: filters.status || undefined, q: filters.q || undefined }),
  });

  const profiles = useQuery({
    queryKey: ["profiles"],
    queryFn: () => api.get<{ items: Profile[] }>("/profiles"),
  });

  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["users"] });
  const rows = users.data?.items ?? [];

  const resetPassword = async (user: User) => {
    try {
      const result = await api.post<{ temporary_password: string }>(`/users/${user.id}/reset-password`);
      setTemporary([{ email: user.email, password: result.temporary_password }]);
      refresh();
    } catch (error) {
      push({ tone: "error", title: errorMessage(error) });
    }
  };

  const revokeSessions = async (user: User) => {
    try {
      const result = await api.post<{ revoked: number }>(`/users/${user.id}/revoke-sessions`);
      push({ tone: "success", title: `${t("users.revokeSessions")} · ${result.revoked}` });
      refresh();
    } catch (error) {
      push({ tone: "error", title: errorMessage(error) });
    }
  };

  const toggleActive = async (user: User) => {
    if (user.is_active && !window.confirm(t("confirm.deactivate", { name: user.name }))) return;
    try {
      await api.post(`/users/${user.id}/${user.is_active ? "deactivate" : "reactivate"}`);
      refresh();
    } catch (error) {
      push({ tone: "error", title: errorMessage(error) });
    }
  };

  const sections: { key: "manager" | "maker" | "reviewer"; title: string; rows: User[] }[] = [
    { key: "manager", title: t("users.sections.managers"), rows: rows.filter((user) => user.role === "manager") },
    { key: "maker", title: t("users.sections.makers"), rows: rows.filter((user) => user.role === "maker") },
    { key: "reviewer", title: t("users.sections.reviewers"), rows: rows.filter((user) => user.role === "reviewer") },
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{t("users.title")}</h1>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Input
            className="h-8 w-48 text-sm"
            placeholder={t("users.searchPlaceholder")}
            value={filters.q}
            onChange={(event) => setFilters({ ...filters, q: event.target.value })}
          />
          <Select className="h-8 w-32 text-xs" value={filters.role} onChange={(event) => setFilters({ ...filters, role: event.target.value })}>
            <option value="">{t("users.filterRole")}: {t("common.all")}</option>
            {["maker", "manager", "reviewer"].map((role) => (
              <option key={role} value={role}>
                {role}
              </option>
            ))}
          </Select>
          <Select className="h-8 w-36 text-xs" value={filters.status} onChange={(event) => setFilters({ ...filters, status: event.target.value })}>
            <option value="">{t("users.filterStatus")}: {t("common.all")}</option>
            <option value="active">{t("users.status.active")}</option>
            <option value="deactivated">{t("users.status.deactivated")}</option>
          </Select>
          <Button size="sm" variant="outline" onClick={() => setImportOpen(true)}>
            <Upload className="h-3.5 w-3.5" />
            {t("users.import")}
          </Button>
          <Button size="sm" onClick={() => setCreating(true)}>
            <Plus className="h-3.5 w-3.5" />
            {t("users.create")}
          </Button>
        </div>
      </div>

      {users.isLoading ? (
        <Card>
          <p className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Spinner /> {t("common.loading")}
          </p>
        </Card>
      ) : users.isError ? (
        <Card>
          <ErrorNote>{errorMessage(users.error)}</ErrorNote>
        </Card>
      ) : rows.length === 0 ? (
        <Card>
          <EmptyState title={t("common.noResults")} />
        </Card>
      ) : (
        sections
          .filter((section) => section.rows.length > 0)
          .map((section) => (
            <section key={section.key} className="space-y-2">
              <h2 className="text-sm font-semibold">
                {section.title} <span className="text-muted-foreground">({section.rows.length})</span>
              </h2>
              <div className="grid gap-3 tablet:grid-cols-2 xl:grid-cols-3">
                {section.rows.map((user) => (
                  <UserCard
                    key={user.id}
                    user={user}
                    onEdit={() => setEditing(user)}
                    onResetPassword={() => void resetPassword(user)}
                    onRevokeSessions={() => void revokeSessions(user)}
                    onToggleActive={() => void toggleActive(user)}
                  />
                ))}
              </div>
            </section>
          ))
      )}

      <UserDialog
        open={creating || Boolean(editing)}
        user={editing}
        profiles={profiles.data?.items ?? []}
        onClose={() => {
          setCreating(false);
          setEditing(null);
        }}
        onSaved={(passwords) => {
          setCreating(false);
          setEditing(null);
          refresh();
          if (passwords) setTemporary(passwords);
        }}
        push={push}
      />

      <Dialog
        open={Boolean(temporary)}
        onClose={() => setTemporary(null)}
        title={t("users.temporaryPassword")}
        description={t("users.temporaryPasswordHint")}
        width="max-w-lg"
        footer={
          <Button onClick={() => setTemporary(null)}>{t("common.close")}</Button>
        }
      >
        <ul className="space-y-2">
          {(temporary ?? []).map((entry) => (
            <li key={entry.email} className="flex items-center justify-between gap-2 rounded border border-border px-3 py-2">
              <span className="text-sm">{entry.email}</span>
              <span className="flex items-center gap-2">
                <code className="rounded bg-muted px-2 py-0.5 font-mono text-xs">{entry.password}</code>
                <Button
                  size="icon"
                  variant="ghost"
                  onClick={() => {
                    void navigator.clipboard.writeText(entry.password);
                    push({ tone: "success", title: t("common.copied") });
                  }}
                >
                  <Copy className="h-3.5 w-3.5" />
                </Button>
              </span>
            </li>
          ))}
        </ul>
      </Dialog>

      <ImportDialog open={importOpen} onClose={() => setImportOpen(false)} onImported={(created) => { refresh(); setImportOpen(false); setTemporary(created); }} />
    </div>
  );
}

function UserDialog({
  open,
  user,
  profiles,
  onClose,
  onSaved,
  push,
}: {
  open: boolean;
  user: User | null;
  profiles: Profile[];
  onClose: () => void;
  onSaved: (temporary: { email: string; password: string }[] | null) => void;
  push: ReturnType<typeof useToast>["push"];
}) {
  const [draft, setDraft] = useState({
    name: "",
    email: "",
    role: "maker",
    daily_limit: "" as string,
    profile_id: "",
    must_change_password: true,
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setDraft({
      name: user?.name ?? "",
      email: user?.email ?? "",
      role: user?.role ?? "maker",
      daily_limit: user?.daily_limit !== null && user?.daily_limit !== undefined ? String(user.daily_limit) : "",
      profile_id: user?.profile_id ?? "",
      must_change_password: user?.must_change_password ?? true,
    });
    setError(null);
  }, [open, user]);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const payload = {
        name: draft.name,
        email: draft.email,
        role: draft.role,
        daily_limit: draft.role === "maker" && draft.daily_limit !== "" ? Number(draft.daily_limit) : null,
        profile_id: draft.role === "maker" && draft.profile_id ? draft.profile_id : null,
        must_change_password: draft.must_change_password,
      };
      if (user) {
        await api.patch(`/users/${user.id}`, payload);
        push({ tone: "success", title: t("users.updated") });
        onSaved(null);
      } else {
        const result = await api.post<{ user: User; initial_password: string }>("/users", payload);
        push({ tone: "success", title: t("users.created") });
        onSaved([{ email: result.user.email, password: result.initial_password }]);
      }
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={user ? t("common.edit") : t("users.create")}
      width="max-w-xl"
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            {t("common.cancel")}
          </Button>
          <Button onClick={() => void submit()} loading={busy} disabled={!draft.name || !draft.email}>
            {t("common.save")}
          </Button>
        </>
      }
    >
      <div className="grid gap-3 tablet:grid-cols-2">
        <Field label={t("common.name")}>
          <Input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
        </Field>
        <Field label={t("common.email")}>
          <Input type="email" value={draft.email} onChange={(event) => setDraft({ ...draft, email: event.target.value })} />
        </Field>
        <Field label={t("common.role")}>
          <Select value={draft.role} onChange={(event) => setDraft({ ...draft, role: event.target.value })}>
            {["maker", "manager", "reviewer"].map((role) => (
              <option key={role} value={role}>
                {role}
              </option>
            ))}
          </Select>
        </Field>
        {draft.role === "maker" ? (
          <>
            <Field label={t("users.dailyLimit")}>
              <Input
                type="number"
                value={draft.daily_limit}
                onChange={(event) => setDraft({ ...draft, daily_limit: event.target.value })}
              />
            </Field>
            <Field label={t("users.profile")}>
              <Select value={draft.profile_id} onChange={(event) => setDraft({ ...draft, profile_id: event.target.value })}>
                <option value="">{t("common.none")}</option>
                {profiles.map((profile) => (
                  <option key={profile.id} value={profile.id}>
                    {profile.name}
                  </option>
                ))}
              </Select>
            </Field>
          </>
        ) : null}
      </div>
      <Checkbox
        label={t("users.mustChange")}
        checked={draft.must_change_password}
        onChange={(event) => setDraft({ ...draft, must_change_password: event.target.checked })}
      />
      {!user ? <p className="mt-2 text-xs text-muted-foreground">{t("users.temporaryPasswordHint")}</p> : null}
      {error ? (
        <div className="mt-3">
          <ErrorNote>{error}</ErrorNote>
        </div>
      ) : null}
    </Dialog>
  );
}

function ImportDialog({
  open,
  onClose,
  onImported,
}: {
  open: boolean;
  onClose: () => void;
  onImported: (created: { email: string; password: string }[]) => void;
}) {
  const [csv, setCsv] = useState("name,email,role,profile,limit\n");
  const [result, setResult] = useState<{ created: { email: string; role: string; temporary_password: string }[]; errors: { row: number; error: string; email?: string }[] } | null>(null);
  const [busy, setBusy] = useState(false);

  const preview = useMemo(() => csv.trim().split("\n").slice(1).filter(Boolean).length, [csv]);

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={t("users.import")}
      description={t("users.importHint")}
      width="max-w-2xl"
      footer={
        <>
          <Button variant="outline" onClick={onClose}>
            {t("common.close")}
          </Button>
          <Button
            disabled={busy || preview === 0}
            onClick={async () => {
              setBusy(true);
              try {
                const response = await api.post<{ created: { email: string; role: string; temporary_password: string }[]; errors: { row: number; error: string }[] }>(
                  "/users/import",
                  { csv },
                );
                setResult(response);
                onImported(response.created.map((row) => ({ email: row.email, password: row.temporary_password })));
              } finally {
                setBusy(false);
              }
            }}
          >
            <RefreshCw className="h-3.5 w-3.5" />
            {t("users.importRun", { count: preview })}
          </Button>
        </>
      }
    >
      <textarea
        className="rf-input min-h-[180px] font-mono text-xs"
        value={csv}
        onChange={(event) => setCsv(event.target.value)}
        spellCheck={false}
      />
      {result ? (
        <div className="mt-3 space-y-2 text-sm">
          <p>{t("common.total", { count: result.created.length })}</p>
          {result.errors.length ? (
            <div>
              <p className="text-destructive">{t("users.importErrors", { count: result.errors.length })}</p>
              <ul className="text-xs">
                {result.errors.map((error, index) => (
                  <li key={index}>
                    #{error.row}: {error.error}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : null}
    </Dialog>
  );
}
