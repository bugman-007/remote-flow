import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, Plus, Save, UserPlus } from "lucide-react";
import { api, errorMessage } from "../lib/api";
import { t } from "../i18n";
import { Badge, Button, Card, CardHeader, Checkbox, EmptyState, ErrorNote, Field, Input, Select, Spinner, Textarea } from "../ui/primitives";
import { Dialog, Drawer } from "../ui/dialog";
import { Table } from "../ui/table";
import { useToast } from "../ui/toast";
import { formatDate, formatDateTime } from "../lib/format";
import { parseList } from "../lib/utils";
import type { Paginated, Profile, PromptVersion, Theme, User } from "../types";

const SHAREABLE = ["Name", "URL", "Description", "Start date", "End date", "Tags"];

export function ProfilesPage() {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [openId, setOpenId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");

  const profiles = useQuery({
    queryKey: ["profiles", { q: search }],
    queryFn: () => api.get<{ items: Profile[] }>("/profiles", { q: search || undefined }),
  });

  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["profiles"] });
  const rows = profiles.data?.items ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{t("profiles.title")}</h1>
          <p className="text-sm text-muted-foreground">{t("profiles.snapshotNote")}</p>
        </div>
        <div className="flex items-center gap-2">
          <Input className="h-8 w-48 text-sm" placeholder={t("common.search")} value={search} onChange={(event) => setSearch(event.target.value)} />
          <Button size="sm" onClick={() => setCreating(true)}>
            <Plus className="h-3.5 w-3.5" />
            {t("profiles.create")}
          </Button>
        </div>
      </div>

      <Card>
        {profiles.isLoading ? (
          <p className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Spinner /> {t("common.loading")}
          </p>
        ) : profiles.isError ? (
          <ErrorNote>{errorMessage(profiles.error)}</ErrorNote>
        ) : rows.length === 0 ? (
          <EmptyState title={t("profiles.empty")} />
        ) : (
          <Table>
            <thead>
              <tr>
                <th>{t("common.name")}</th>
                <th>{t("profiles.url")}</th>
                <th>{t("common.status")}</th>
                <th>{t("profiles.theme")}</th>
                <th>{t("profiles.provider")}</th>
                <th>{t("profiles.makers")}</th>
                <th>{t("profiles.docSets")}</th>
                <th>{t("common.updated")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((profile) => (
                <tr key={profile.id} className="cursor-pointer hover:bg-accent/40" onClick={() => setOpenId(profile.id)}>
                  <td className="font-medium">{profile.name}</td>
                  <td className="max-w-[14rem] truncate text-xs">{profile.url ?? "—"}</td>
                  <td>
                    <Badge tone={profile.status === "active" ? "success" : "neutral"}>
                      {profile.status === "active" ? t("common.active") : t("common.archived")}
                    </Badge>
                  </td>
                  <td>{profile.theme_name ?? "—"}</td>
                  <td>{profile.provider_name ?? t("common.none")}</td>
                  <td>{profile.maker_count}</td>
                  <td>{profile.doc_set_count}</td>
                  <td className="text-xs">{formatDateTime(profile.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <ProfileDialog
        open={creating || Boolean(openId)}
        profileId={openId}
        onClose={() => {
          setCreating(false);
          setOpenId(null);
        }}
        onSaved={() => {
          refresh();
          setCreating(false);
        }}
        push={push}
      />
    </div>
  );
}

function ProfileDialog({
  open,
  profileId,
  onClose,
  onSaved,
  push,
}: {
  open: boolean;
  profileId: string | null;
  onClose: () => void;
  onSaved: () => void;
  push: ReturnType<typeof useToast>["push"];
}) {
  const [tab, setTab] = useState<"details" | "prompt" | "makers" | "share">("details");
  const [draft, setDraft] = useState<Profile | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const profile = useQuery({
    queryKey: ["profile", profileId],
    queryFn: () => api.get<Profile>(`/profiles/${profileId}`),
    enabled: open && Boolean(profileId),
  });

  const themes = useQuery({
    queryKey: ["themes"],
    queryFn: () => api.get<{ items: Theme[] }>("/themes"),
    enabled: open,
  });

  const providers = useQuery({
    queryKey: ["providers"],
    queryFn: () => api.get<{ items: { id: string; display_name: string; is_enabled: boolean }[] }>("/providers"),
    enabled: open,
  });

  useEffect(() => {
    if (profile.data) setDraft(profile.data);
  }, [profile.data]);

  useEffect(() => {
    if (open && !profileId) {
      setDraft({
        id: "",
        name: "",
        url: "",
        description: "",
        start_date: null,
        end_date: null,
        theme_id: null,
        provider_id: null,
        model: null,
        temperature: null,
        max_tokens: null,
        tags: [],
        custom_fields: {},
        shared_fields: ["Name", "URL"],
        status: "active",
        active_prompt_version_id: null,
        active_prompt: null,
        maker_count: 0,
        doc_set_count: 0,
        created_at: "",
        updated_at: "",
      });
      setTab("details");
    }
  }, [open, profileId]);

  const save = async () => {
    if (!draft) return;
    setBusy(true);
    setError(null);
    try {
      const payload = {
        name: draft.name,
        url: draft.url,
        description: draft.description,
        start_date: draft.start_date,
        end_date: draft.end_date,
        theme_id: draft.theme_id,
        provider_id: draft.provider_id,
        model: draft.model,
        temperature: draft.temperature,
        max_tokens: draft.max_tokens,
        tags: draft.tags,
        custom_fields: draft.custom_fields,
        shared_fields: draft.shared_fields,
      };
      if (profileId) await api.patch(`/profiles/${profileId}`, payload);
      else await api.post("/profiles", payload);
      push({ tone: "success", title: t("toast.saved") });
      onSaved();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width="max-w-4xl"
      title={draft ? draft.name || t("profiles.create") : t("profiles.title")}
    >
      {profile.isLoading ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> {t("common.loading")}
        </p>
      ) : !draft ? null : (
        <div className="space-y-4">
          <div className="flex flex-wrap gap-2">
            {(["details", "prompt", "makers", "share"] as const).map((value) => (
              <Button key={value} size="sm" variant={tab === value ? "primary" : "outline"} onClick={() => setTab(value)}>
                {value === "details"
                  ? t("common.description")
                  : value === "prompt"
                    ? t("profiles.prompt")
                    : value === "makers"
                      ? t("profiles.assignments")
                      : t("profiles.share")}
              </Button>
            ))}
            <span className="ml-auto flex items-center gap-2">
              {profileId ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={async () => {
                    try {
                      await api.post(`/profiles/${profileId}/archive`);
                      push({ tone: "success", title: t("toast.updated") });
                      onSaved();
                    } catch (caught) {
                      push({ tone: "error", title: errorMessage(caught) });
                    }
                  }}
                >
                  <Archive className="h-3.5 w-3.5" />
                  {draft.status === "archived" ? t("profiles.unarchive") : t("profiles.archive")}
                </Button>
              ) : null}
              <Button size="sm" onClick={() => void save()} disabled={busy}>
                <Save className="h-3.5 w-3.5" />
                {busy ? t("common.saving") : t("common.save")}
              </Button>
            </span>
          </div>

          {error ? <ErrorNote>{error}</ErrorNote> : null}

          {tab === "details" ? (
            <div className="grid gap-3 tablet:grid-cols-2">
              <Field label={t("common.name")}>
                <Input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
              </Field>
              <Field label={t("profiles.url")}>
                <Input value={draft.url ?? ""} onChange={(event) => setDraft({ ...draft, url: event.target.value })} />
              </Field>
              <Field label={t("common.description")} className="tablet:col-span-2">
                <Textarea value={draft.description ?? ""} onChange={(event) => setDraft({ ...draft, description: event.target.value })} />
              </Field>
              <Field label={t("profiles.startDate")}>
                <Input type="date" value={draft.start_date ?? ""} onChange={(event) => setDraft({ ...draft, start_date: event.target.value })} />
              </Field>
              <Field label={t("profiles.endDate")}>
                <Input type="date" value={draft.end_date ?? ""} onChange={(event) => setDraft({ ...draft, end_date: event.target.value })} />
              </Field>
              <Field label={t("profiles.theme")}>
                <Select value={draft.theme_id ?? ""} onChange={(event) => setDraft({ ...draft, theme_id: event.target.value || null })}>
                  <option value="">{t("common.none")}</option>
                  {(themes.data?.items ?? []).map((theme) => (
                    <option key={theme.id} value={theme.id}>
                      {theme.name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label={t("profiles.provider")}>
                <Select value={draft.provider_id ?? ""} onChange={(event) => setDraft({ ...draft, provider_id: event.target.value || null })}>
                  <option value="">{t("common.none")}</option>
                  {(providers.data?.items ?? []).map((provider) => (
                    <option key={provider.id} value={provider.id}>
                      {provider.display_name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label={t("profiles.model")}>
                <Input value={draft.model ?? ""} onChange={(event) => setDraft({ ...draft, model: event.target.value })} />
              </Field>
              <Field label={t("profiles.temperature")}>
                <Input
                  type="number"
                  step="0.1"
                  value={draft.temperature ?? ""}
                  onChange={(event) => setDraft({ ...draft, temperature: event.target.value === "" ? null : Number(event.target.value) })}
                />
              </Field>
              <Field label={t("profiles.maxTokens")}>
                <Input
                  type="number"
                  value={draft.max_tokens ?? ""}
                  onChange={(event) => setDraft({ ...draft, max_tokens: event.target.value === "" ? null : Number(event.target.value) })}
                />
              </Field>
              <Field label={t("profiles.tags")} hint={t("profiles.tagsHint")}>
                <Input value={draft.tags.join(", ")} onChange={(event) => setDraft({ ...draft, tags: parseList(event.target.value) })} />
              </Field>
              <div className="tablet:col-span-2">
                <p className="rf-label">{t("profiles.customFields")}</p>
                {Object.entries(draft.custom_fields).map(([key, value]) => (
                  <div key={key} className="mb-2 flex items-center gap-2">
                    <Input value={key} readOnly className="w-40 bg-muted" />
                    <Input
                      value={value}
                      onChange={(event) => setDraft({ ...draft, custom_fields: { ...draft.custom_fields, [key]: event.target.value } })}
                    />
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        const next = { ...draft.custom_fields };
                        delete next[key];
                        setDraft({ ...draft, custom_fields: next });
                      }}
                    >
                      {t("common.delete")}
                    </Button>
                  </div>
                ))}
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => setDraft({ ...draft, custom_fields: { ...draft.custom_fields, [`field_${Object.keys(draft.custom_fields).length + 1}`]: "" } })}
                >
                  <Plus className="h-3.5 w-3.5" />
                  {t("profiles.addCustomField")}
                </Button>
              </div>
              <p className="text-xs text-muted-foreground tablet:col-span-2">{t("profiles.snapshotNote")}</p>
            </div>
          ) : null}

          {tab === "prompt" ? <PromptTab profileId={profileId} draft={draft} push={push} /> : null}
          {tab === "makers" ? <MakersTab profileId={profileId} push={push} /> : null}

          {tab === "share" ? (
            <div className="space-y-2">
              <p className="text-sm text-muted-foreground">{t("profiles.shareHint")}</p>
              {SHAREABLE.map((field) => (
                <Checkbox
                  key={field}
                  label={field}
                  checked={draft.shared_fields.includes(field)}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      shared_fields: event.target.checked
                        ? [...draft.shared_fields, field]
                        : draft.shared_fields.filter((value) => value !== field),
                    })
                  }
                />
              ))}
              {Object.keys(draft.custom_fields).map((field) => (
                <Checkbox
                  key={field}
                  label={`Custom: ${field}`}
                  checked={draft.shared_fields.includes(`Custom: ${field}`)}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      shared_fields: event.target.checked
                        ? [...draft.shared_fields, `Custom: ${field}`]
                        : draft.shared_fields.filter((value) => value !== `Custom: ${field}`),
                    })
                  }
                />
              ))}
            </div>
          ) : null}
        </div>
      )}
    </Drawer>
  );
}

function PromptTab({
  profileId,
  draft,
  push,
}: {
  profileId: string | null;
  draft: Profile;
  push: ReturnType<typeof useToast>["push"];
}) {
  const queryClient = useQueryClient();
  const [body, setBody] = useState(draft.active_prompt?.body ?? "");
  const [note, setNote] = useState("");
  const [testJd, setTestJd] = useState("");
  const [testResult, setTestResult] = useState<{ json: unknown; call_log: unknown; candidate_name: string } | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const [openDiff, setOpenDiff] = useState<string | null>(null);

  const versions = useQuery({
    queryKey: ["prompt-versions", profileId],
    queryFn: () => api.get<{ items: PromptVersion[] }>(`/profiles/${profileId}/prompt-versions`),
    enabled: Boolean(profileId),
  });

  const saveVersion = async () => {
    if (!profileId) return;
    try {
      await api.post(`/profiles/${profileId}/prompt-versions`, { body, change_note: note });
      setNote("");
      push({ tone: "success", title: t("toast.saved") });
      await queryClient.invalidateQueries({ queryKey: ["prompt-versions", profileId] });
      await queryClient.invalidateQueries({ queryKey: ["profile", profileId] });
    } catch (error) {
      push({ tone: "error", title: errorMessage(error) });
    }
  };

  const runTest = async () => {
    if (!profileId) return;
    setTestError(null);
    try {
      const result = await api.post<{ json: unknown; call_log: unknown; candidate_name: string }>(`/profiles/${profileId}/test`, {
        jd_text: testJd,
      });
      setTestResult(result);
    } catch (error) {
      setTestResult(null);
      setTestError(errorMessage(error));
    }
  };

  return (
    <div className="space-y-3">
      <Field label={t("profiles.prompt")} hint={t("profiles.promptPlaceholders")}>
        <Textarea
          className="min-h-[240px] font-mono text-xs"
          value={body}
          onChange={(event) => setBody(event.target.value)}
        />
      </Field>
      <div className="flex flex-wrap items-end gap-2">
        <Field className="mb-0 flex-1" label={t("profiles.changeNote")}>
          <Input value={note} onChange={(event) => setNote(event.target.value)} />
        </Field>
        <Button onClick={() => void saveVersion()} disabled={!profileId}>
          {t("profiles.saveVersion")}
        </Button>
      </div>

      <Card>
        <CardHeader title={t("profiles.versions")} />
        <ul className="space-y-1 text-sm">
          {(versions.data?.items ?? []).map((version) => (
            <li key={version.id} className="space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={version.active ? "success" : "outline"}>v{version.version_no}</Badge>
                {version.active ? <Badge tone="info">{t("profiles.activeVersion")}</Badge> : null}
                <span className="text-xs text-muted-foreground">{formatDateTime(version.created_at)}</span>
                {version.author_name ? (
                  <span className="text-xs text-muted-foreground">
                    {t("profiles.versionAuthor", { name: version.author_name })}
                  </span>
                ) : null}
                <span className="flex-1 truncate text-xs">{version.change_note ?? ""}</span>
                <Button size="sm" variant="ghost" onClick={() => setOpenDiff(openDiff === version.id ? null : version.id)}>
                  {openDiff === version.id ? t("profiles.hideDiff") : t("profiles.showDiff")}
                </Button>
                {!version.active ? (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={async () => {
                      await api.post(`/profiles/${profileId}/prompt-versions/${version.id}/activate`);
                      await queryClient.invalidateQueries({ queryKey: ["prompt-versions", profileId] });
                      await queryClient.invalidateQueries({ queryKey: ["profile", profileId] });
                    }}
                  >
                    {t("profiles.activate")}
                  </Button>
                ) : null}
              </div>
              {openDiff === version.id ? (
                <div className="max-h-64 overflow-auto rounded-md border border-border bg-muted/40 p-2 font-mono text-xs">
                  {version.diff?.length ? (
                    version.diff.map((entry, index) => (
                      <div
                        key={index}
                        className={
                          entry.op === "add"
                            ? "text-emerald-600 dark:text-emerald-400"
                            : entry.op === "remove"
                              ? "text-destructive"
                              : entry.op === "hunk"
                                ? "text-muted-foreground"
                                : ""
                        }
                      >
                        {entry.op === "add" ? "+ " : entry.op === "remove" ? "- " : "  "}
                        {entry.line || "\u00a0"}
                      </div>
                    ))
                  ) : (
                    <span className="text-muted-foreground">{t("profiles.firstVersion")}</span>
                  )}
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      </Card>

      <Card>
        <CardHeader title={t("profiles.testPrompt")} description={t("profiles.contract")} />
        <Field label={t("profiles.testJd")}>
          <Textarea value={testJd} onChange={(event) => setTestJd(event.target.value)} />
        </Field>
        <Button size="sm" onClick={() => void runTest()} disabled={!profileId || testJd.trim().length < 50}>
          {t("profiles.runTest")}
        </Button>
        {testError ? (
          <div className="mt-2">
            <ErrorNote>{testError}</ErrorNote>
          </div>
        ) : null}
        {testResult ? (
          <div className="mt-3 space-y-2">
            <Badge tone="success">{t("profiles.valid")}</Badge>
            <p className="text-xs text-muted-foreground">
              {testResult.candidate_name} · {t("resumes.detail.usage", { tokens: "—", latency: "—" })}
            </p>
            <pre className="max-h-64 overflow-auto rounded bg-muted p-2 font-mono text-xs">{JSON.stringify(testResult.json, null, 2)}</pre>
          </div>
        ) : null}
      </Card>

      <p className="text-xs text-muted-foreground">{t("profiles.snapshotNote")}</p>
      <span className="hidden">{draft.id}</span>
    </div>
  );
}

function MakersTab({ profileId, push }: { profileId: string | null; push: ReturnType<typeof useToast>["push"] }) {
  const queryClient = useQueryClient();
  const [assignOpen, setAssignOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);

  const assignments = useQuery({
    queryKey: ["assignments", profileId],
    queryFn: () =>
      api.get<{ items: { assignment_id: string; maker_id: string; maker_name: string; daily_limit: number | null; assigned_at: string; usage_today: number }[] }>(
        `/profiles/${profileId}/assignments`,
      ),
    enabled: Boolean(profileId),
  });

  const makers = useQuery({
    queryKey: ["users", { role: "maker", all: true }],
    queryFn: () => api.get<Paginated<User & { profile_name?: string }>>("/users", { role: "maker", page_size: 200 }),
    enabled: assignOpen,
  });

  if (!profileId) return <p className="text-sm text-muted-foreground">{t("common.save")}…</p>;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">{t("profiles.assignHint")}</p>
        <Button size="sm" onClick={() => setAssignOpen(true)}>
          <UserPlus className="h-3.5 w-3.5" />
          {t("profiles.assign")}
        </Button>
      </div>
      <Table>
        <thead>
          <tr>
            <th>{t("common.name")}</th>
            <th>{t("profiles.dailyLimit")}</th>
            <th>{t("profiles.usageToday")}</th>
            <th>{t("profiles.assignedAt")}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(assignments.data?.items ?? []).map((row) => (
            <tr key={row.assignment_id}>
              <td>{row.maker_name}</td>
              <td>{row.daily_limit ?? "—"}</td>
              <td>{row.usage_today}</td>
              <td className="text-xs">{formatDate(row.assigned_at)}</td>
              <td className="text-right">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={async () => {
                    await api.post(`/profiles/assignments/${row.assignment_id}/end`);
                    await queryClient.invalidateQueries({ queryKey: ["assignments", profileId] });
                    push({ tone: "success", title: t("toast.updated") });
                  }}
                >
                  {t("profiles.unassign")}
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </Table>

      <Dialog
        open={assignOpen}
        onClose={() => setAssignOpen(false)}
        title={t("profiles.assign")}
        footer={
          <>
            <Button variant="outline" onClick={() => setAssignOpen(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              onClick={async () => {
                try {
                  await api.post(`/profiles/${profileId}/assignments`, { maker_ids: selected });
                  setAssignOpen(false);
                  setSelected([]);
                  await queryClient.invalidateQueries({ queryKey: ["assignments", profileId] });
                  push({ tone: "success", title: t("toast.updated") });
                } catch (error) {
                  push({ tone: "error", title: errorMessage(error) });
                }
              }}
            >
              {t("common.confirm")}
            </Button>
          </>
        }
      >
        <p className="mb-2 text-xs text-muted-foreground">{t("profiles.assignHint")}</p>
        {(makers.data?.items ?? []).map((maker) => (
          <div key={maker.id} className="flex items-center gap-2 py-1">
            <Checkbox
              checked={selected.includes(maker.id)}
              onChange={(event) =>
                setSelected(event.target.checked ? [...selected, maker.id] : selected.filter((value) => value !== maker.id))
              }
            />
            <span className="text-sm">{maker.name}</span>
            {maker.profile_name ? <span className="text-xs text-muted-foreground">{t("profiles.willMove", { profile: maker.profile_name })}</span> : null}
          </div>
        ))}
      </Dialog>
    </div>
  );
}
