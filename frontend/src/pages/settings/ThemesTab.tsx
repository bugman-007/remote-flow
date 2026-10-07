import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, Plus } from "lucide-react";
import { api, errorMessage } from "../../lib/api";
import { Badge, Button, Card, EmptyState, ErrorNote, Spinner } from "../../ui/primitives";
import { Table } from "../../ui/table";
import { useToast } from "../../ui/toast";
import { t } from "../../i18n";
import type { Theme, ThemeSchema } from "../../types";
import { ThemeEditor } from "./ThemeEditor";

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
    queryFn: () => api.get<ThemeSchema>("/themes/schema"),
    staleTime: Infinity,
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

      <ThemeEditor
        open={creating || Boolean(editing)}
        theme={editing}
        schema={schema.data}
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
