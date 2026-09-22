import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorMessage } from "../../lib/api";
import { Button, Card, CardHeader, Checkbox, ErrorNote, Field, Input, Select, Spinner } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import { t } from "../../i18n";
import type { InterviewTemplate, Settings } from "../../types";

const FIELDS: { key: keyof Settings; label: string; type?: "number" | "text" | "checkbox" }[] = [
  { key: "timezone", label: "settings.general.timezone", type: "text" },
  { key: "default_daily_limit", label: "settings.general.defaultDailyLimit", type: "number" },
  { key: "min_jd_chars", label: "settings.general.minJd", type: "number" },
  { key: "max_jd_chars", label: "settings.general.maxJd", type: "number" },
  { key: "llm_timeout_s", label: "settings.general.llmTimeout", type: "number" },
  { key: "render_timeout_s", label: "settings.general.renderTimeout", type: "number" },
  { key: "max_llm_attempts", label: "settings.general.maxLlmAttempts", type: "number" },
  { key: "max_render_attempts", label: "settings.general.maxRenderAttempts", type: "number" },
  { key: "show_selection_to_makers", label: "settings.general.showSelection", type: "checkbox" },
];

/** SET-12/13: general settings, written to the audit log with before/after values. */
export function GeneralTab() {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Settings | null>(null);
  const [error, setError] = useState<string | null>(null);

  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.get<{ values: Settings; schema: string[] }>("/settings"),
  });

  const templates = useQuery({
    queryKey: ["interview-templates"],
    queryFn: () => api.get<{ items: InterviewTemplate[] }>("/interview-templates"),
  });

  useEffect(() => {
    if (settings.data) setDraft(settings.data.values);
  }, [settings.data]);

  if (settings.isLoading || !draft) {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <Spinner /> {t("common.loading")}
      </p>
    );
  }

  const save = async () => {
    setError(null);
    try {
      await api.patch("/settings", { values: draft });
      push({ tone: "success", title: t("settings.general.saved") });
      await queryClient.invalidateQueries({ queryKey: ["settings"] });
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };

  return (
    <Card>
      <CardHeader title={t("settings.tabs.general")} description={t("profiles.snapshotNote")} actions={<Button size="sm" onClick={() => void save()}>{t("common.save")}</Button>} />
      <div className="grid gap-3 tablet:grid-cols-2">
        {FIELDS.map((field) =>
          field.type === "checkbox" ? (
            <Field key={String(field.key)} label={t(field.label)}>
              <Checkbox
                checked={Boolean(draft[field.key])}
                onChange={(event) => setDraft({ ...draft, [field.key]: event.target.checked })}
              />
            </Field>
          ) : (
            <Field key={String(field.key)} label={t(field.label)}>
              <Input
                type={field.type === "number" ? "number" : "text"}
                value={String(draft[field.key] ?? "")}
                onChange={(event) =>
                  setDraft({ ...draft, [field.key]: field.type === "number" ? Number(event.target.value) : event.target.value })
                }
              />
            </Field>
          ),
        )}
        <Field label={t("settings.general.defaultTemplate")}>
          <Select
            value={draft.default_interview_template_id ?? ""}
            onChange={(event) => setDraft({ ...draft, default_interview_template_id: event.target.value || null })}
          >
            <option value="">{t("common.none")}</option>
            {(templates.data?.items ?? []).map((template) => (
              <option key={template.id} value={template.id}>
                {template.name}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      {error ? <ErrorNote>{error}</ErrorNote> : null}
    </Card>
  );
}
