import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, errorMessage } from "../../lib/api";
import { Button, Card, CardHeader, ErrorNote, Field, Input, Spinner } from "../../ui/primitives";
import { Table } from "../../ui/table";
import { t } from "../../i18n";
import { formatBytes } from "../../lib/format";
import type { RetentionPlan, Settings } from "../../types";

const FIELDS: { key: keyof Settings; label: string }[] = [
  { key: "retention_attempt_days", label: "settings.retention.attemptDays" },
  { key: "retention_superseded_days", label: "settings.retention.supersededDays" },
  { key: "retention_doc_set_days", label: "settings.retention.docSetDays" },
  { key: "retention_interview_days", label: "settings.retention.interviewDays" },
  { key: "disk_warn_pct", label: "settings.retention.warnPct" },
  { key: "disk_pause_intake_pct", label: "settings.retention.pauseIntakePct" },
  { key: "disk_pause_render_pct", label: "settings.retention.pauseRenderPct" },
];

/** SET-15: retention windows, disk thresholds, a dry run and the protected list. */
export function RetentionTab() {
  const [draft, setDraft] = useState<Settings | null>(null);
  const [plan, setPlan] = useState<RetentionPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.get<{ values: Settings; schema: string[] }>("/settings"),
  });

  useEffect(() => {
    if (settings.data) setDraft(settings.data.values);
  }, [settings.data]);

  if (!draft) {
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
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };

  return (
    <div className="space-y-3">
      <Card>
        <CardHeader
          title={t("settings.tabs.retention")}
          actions={
            <>
              <Button size="sm" variant="outline" onClick={() => void save()}>
                {t("common.save")}
              </Button>
              <Button
                size="sm"
                onClick={async () => {
                  setBusy(true);
                  setError(null);
                  try {
                    setPlan(await api.post<RetentionPlan>("/retention/dry-run"));
                  } catch (caught) {
                    setError(errorMessage(caught));
                  } finally {
                    setBusy(false);
                  }
                }}
                disabled={busy}
              >
                {t("settings.retention.dryRun")}
              </Button>
            </>
          }
        />
        <div className="grid gap-3 tablet:grid-cols-3">
          {FIELDS.map((field) => (
            <Field key={String(field.key)} label={t(field.label)}>
              <Input
                type="number"
                value={String(draft[field.key] ?? "")}
                onChange={(event) => setDraft({ ...draft, [field.key]: Number(event.target.value) })}
              />
            </Field>
          ))}
        </div>
        {error ? <ErrorNote>{error}</ErrorNote> : null}
        {plan ? (
          <p className="mt-3 text-sm">
            {t("settings.retention.dryRunResult", {
              size: formatBytes(plan.bytes),
              generations: plan.generation_ids.length,
              docSets: plan.doc_set_generations.length,
            })}
          </p>
        ) : null}
      </Card>

      <Card>
        <CardHeader title={t("settings.retention.protected", { count: plan?.protected.length ?? 0 })} />
        {plan?.protected.length ? (
          <Table>
            <thead>
              <tr>
                <th>{t("common.name")}</th>
                <th>{t("common.status")}</th>
                <th>{t("resumes.detail.generations")}</th>
                <th>Bytes</th>
              </tr>
            </thead>
            <tbody>
              {plan.protected.map((item) => (
                <tr key={item.doc_set_id}>
                  <td>{item.label}</td>
                  <td className="text-xs">
                    {item.is_selected ? t("common.selected") : ""} {item.keep ? "Kept" : ""}
                  </td>
                  <td>{item.generations}</td>
                  <td>{formatBytes(item.bytes)}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <p className="text-sm text-muted-foreground">{t("settings.retention.protectedEmpty")}</p>
        )}
      </Card>
    </div>
  );
}
