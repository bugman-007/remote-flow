import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, Plus } from "lucide-react";
import { api, errorMessage } from "../../lib/api";
import { Badge, Button, Card, EmptyState, ErrorNote, Field, Input, Select, Spinner, Textarea } from "../../ui/primitives";
import { Dialog } from "../../ui/dialog";
import { Table } from "../../ui/table";
import { useToast } from "../../ui/toast";
import { t } from "../../i18n";
import type { Profile, Theme, ThemeParams } from "../../types";

interface ThemeField {
  key: string;
  type: string;
  min: number | null;
  max: number | null;
  step: number | null;
  default: unknown;
  options: string[] | null;
  font_labels: { name: string }[] | null;
}

export function ThemesTab() {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<Theme | null>(null);
  const [creating, setCreating] = useState(false);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  const themes = useQuery({
    queryKey: ["themes"],
    queryFn: () => api.get<{ items: Theme[] }>("/themes"),
  });

  const schema = useQuery({
    queryKey: ["theme-schema"],
    queryFn: () => api.get<{ fields: ThemeField[]; defaults: ThemeParams; fonts: { name: string }[] }>("/themes/schema"),
  });

  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["themes"] });
  const rows = themes.data?.items ?? [];

  const preview = async (theme: Theme) => {
    try {
      const blob = await api.requestBlob(`/themes/${theme.id}/preview`, { method: "POST" });
      const url = URL.createObjectURL(blob);
      setPreviewUrl((current) => {
        if (current) URL.revokeObjectURL(current);
        return url;
      });
    } catch (error) {
      push({ tone: "error", title: errorMessage(error) });
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">{t("settings.themes.applies")}</p>
        <Button size="sm" onClick={() => setCreating(true)}>
          <Plus className="h-3.5 w-3.5" />
          {t("settings.themes.create")}
        </Button>
      </div>

      <Card>
        {themes.isLoading || schema.isLoading ? (
          <p className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Spinner /> {t("common.loading")}
          </p>
        ) : themes.isError ? (
          <ErrorNote>{errorMessage(themes.error)}</ErrorNote>
        ) : rows.length === 0 ? (
          <EmptyState title={t("settings.themes.empty")} />
        ) : (
          <Table>
            <thead>
              <tr>
                <th>{t("common.name")}</th>
                <th>{t("common.description")}</th>
                <th>{t("settings.themes.font")}</th>
                <th>{t("settings.themes.accent")}</th>
                <th>{t("common.status")}</th>
                <th>{t("settings.themes.assignedTo", { count: 0 })}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((theme) => (
                <tr key={theme.id} className="hover:bg-accent/40">
                  <td className="font-medium">{theme.name}</td>
                  <td className="max-w-[16rem] truncate text-xs">{theme.description ?? "—"}</td>
                  <td className="text-xs">
                    {String(theme.params.font ?? "—")} · {String(theme.params.size ?? "—")}pt
                  </td>
                  <td>
                    <span className="flex items-center gap-2">
                      <span className="h-4 w-4 rounded border border-border" style={{ background: String(theme.params.accent ?? "#fff") }} />
                      <code className="text-xs">{String(theme.params.accent ?? "—")}</code>
                    </span>
                  </td>
                  <td>
                    <Badge tone={theme.status === "active" ? "success" : "neutral"}>
                      {theme.status === "active" ? t("common.active") : t("common.archived")}
                    </Badge>
                  </td>
                  <td className="text-xs">{theme.profiles.map((profile) => profile.name).join(", ") || "—"}</td>
                  <td>
                    <div className="flex items-center justify-end gap-1">
                      <Button size="sm" variant="ghost" onClick={() => void preview(theme)}>
                        <Eye className="h-3.5 w-3.5" />
                        {t("settings.themes.preview")}
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setEditing(theme)}>
                        {t("common.edit")}
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      {previewUrl ? (
        <Card>
          <div className="mb-2 flex items-center justify-between">
            <p className="text-sm font-medium">{t("settings.themes.preview")}</p>
            <Button size="sm" variant="ghost" onClick={() => { URL.revokeObjectURL(previewUrl); setPreviewUrl(null); }}>
              {t("common.close")}
            </Button>
          </div>
          <iframe title={t("settings.themes.preview")} src={previewUrl} className="h-[70vh] w-full rounded border border-border" />
        </Card>
      ) : null}

      <ThemeDialog
        open={creating || Boolean(editing)}
        theme={editing}
        fields={schema.data?.fields ?? []}
        defaults={schema.data?.defaults ?? {}}
        onClose={() => {
          setCreating(false);
          setEditing(null);
        }}
        onSaved={() => {
          setCreating(false);
          setEditing(null);
          refresh();
        }}
      />
    </div>
  );
}

function ThemeDialog({
  open,
  theme,
  fields,
  defaults,
  onClose,
  onSaved,
}: {
  open: boolean;
  theme: Theme | null;
  fields: ThemeField[];
  defaults: ThemeParams;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [params, setParams] = useState<ThemeParams>({});
  const [assigned, setAssigned] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const profiles = useQuery({
    queryKey: ["profiles"],
    queryFn: () => api.get<{ items: Profile[] }>("/profiles"),
    enabled: open,
  });

  useEffect(() => {
    if (!open) return;
    setName(theme?.name ?? "");
    setDescription(theme?.description ?? "");
    setParams({ ...defaults, ...(theme?.params ?? {}) });
    setAssigned((theme?.profiles ?? []).map((profile) => profile.id));
    setError(null);
  }, [open, theme, defaults]);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const payload = { name, description, params };
      let id = theme?.id ?? null;
      if (id) await api.patch(`/themes/${id}`, payload);
      else {
        const created = await api.post<Theme>("/themes", payload);
        id = created.id;
      }
      if (id) await api.post(`/themes/${id}/assign`, { profile_ids: assigned });
      push({ tone: "success", title: t("toast.saved") });
      await queryClient.invalidateQueries({ queryKey: ["themes"] });
      onSaved();
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
      title={theme ? t("common.edit") : t("settings.themes.create")}
      width="max-w-2xl"
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            {t("common.cancel")}
          </Button>
          <Button onClick={() => void save()} loading={busy} disabled={!name}>
            {t("common.save")}
          </Button>
        </>
      }
    >
      <div className="grid gap-3 tablet:grid-cols-2">
        <Field label={t("common.name")}>
          <Input value={name} onChange={(event) => setName(event.target.value)} />
        </Field>
        <Field label={t("common.description")}>
          <Textarea value={description} onChange={(event) => setDescription(event.target.value)} />
        </Field>
        {fields.map((field) => (
          <Field key={field.key} label={field.key.replace(/_/g, " ")} hint={field.min !== null ? `${field.min} – ${field.max}` : undefined}>
            {field.type === "font" ? (
              <Select value={String(params[field.key] ?? "")} onChange={(event) => setParams({ ...params, [field.key]: event.target.value })}>
                {(field.options ?? []).map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </Select>
            ) : field.type === "color" ? (
              <span className="flex items-center gap-2">
                <input
                  type="color"
                  className="h-9 w-12 rounded border border-input"
                  value={String(params[field.key] ?? "#ffffff")}
                  onChange={(event) => setParams({ ...params, [field.key]: event.target.value })}
                />
                <Input value={String(params[field.key] ?? "")} onChange={(event) => setParams({ ...params, [field.key]: event.target.value })} />
              </span>
            ) : (
              <Input
                type={field.type === "number_pct" || field.type === "number" ? "number" : "text"}
                step={field.step ?? undefined}
                min={field.min ?? undefined}
                max={field.max ?? undefined}
                value={String(params[field.key] ?? "")}
                onChange={(event) =>
                  setParams({ ...params, [field.key]: event.target.value === "" ? null : Number(event.target.value) })
                }
              />
            )}
          </Field>
        ))}
        <Field label={t("settings.themes.assign")} className="tablet:col-span-2">
          <div className="flex flex-wrap gap-3">
            {(profiles.data?.items ?? []).map((profile) => (
              <label key={profile.id} className="flex items-center gap-1 text-sm">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-[hsl(var(--primary))]"
                  checked={assigned.includes(profile.id)}
                  onChange={(event) =>
                    setAssigned(event.target.checked ? [...assigned, profile.id] : assigned.filter((value) => value !== profile.id))
                  }
                />
                {profile.name}
              </label>
            ))}
          </div>
        </Field>
      </div>
      {error ? (
        <div className="mt-3">
          <ErrorNote>{error}</ErrorNote>
        </div>
      ) : null}
      <p className="mt-2 text-xs text-muted-foreground">{t("settings.themes.applies")}</p>
    </Dialog>
  );
}
