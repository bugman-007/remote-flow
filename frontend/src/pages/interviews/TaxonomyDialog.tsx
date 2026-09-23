import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
import { api, errorMessage } from "../../lib/api";
import { Dialog } from "../../ui/dialog";
import { Button, ErrorNote, Input } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import { t } from "../../i18n";
import type { InterviewStatus, InterviewStep } from "../../types";

/** Palette used for step/status swatches (kept in sync with the row tags). */
export const TAXONOMY_COLORS = [
  "#6366f1",
  "#0ea5e9",
  "#10b981",
  "#f59e0b",
  "#ef4444",
  "#8b5cf6",
  "#ec4899",
  "#64748b",
];

const DEFAULT_COLOR = TAXONOMY_COLORS[0];

interface Taxonomy {
  steps: InterviewStep[];
  statuses: InterviewStatus[];
}

/** INT-14: the Manager owns the ordered interview steps and the status labels. */
export function TaxonomyDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const taxonomy = useQuery({
    queryKey: ["interview-taxonomy"],
    queryFn: () => api.get<Taxonomy>("/interview-taxonomy"),
    enabled: open,
  });

  const steps = useMemo(() => taxonomy.data?.steps ?? [], [taxonomy.data]);
  const statuses = useMemo(() => taxonomy.data?.statuses ?? [], [taxonomy.data]);

  const refresh = async () => {
    await taxonomy.refetch();
    void queryClient.invalidateQueries({ queryKey: ["interviews"] });
  };

  const run = async (action: () => Promise<unknown>, message?: string) => {
    setError(null);
    try {
      await action();
      if (message) push({ tone: "success", title: message });
      await refresh();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={t("interviews.stepsDialogTitle")}
      description={t("interviews.stepsDialogHint")}
      width="max-w-2xl"
      footer={
        <Button variant="outline" onClick={onClose}>
          {t("common.close")}
        </Button>
      }
    >
      {taxonomy.isLoading ? (
        <p className="text-sm text-muted-foreground">{t("common.loading")}</p>
      ) : (
        <div className="space-y-5">
          {error ? <ErrorNote>{error}</ErrorNote> : null}
          <TaxonomySection
            kind="steps"
            title={t("interviews.stepsSection")}
            rows={steps}
            onRun={run}
          />
          <TaxonomySection
            kind="statuses"
            title={t("interviews.statusesSection")}
            rows={statuses}
            onRun={run}
          />
        </div>
      )}
    </Dialog>
  );
}

type Row = InterviewStep | InterviewStatus;

function TaxonomySection({
  kind,
  title,
  rows,
  onRun,
}: {
  kind: "steps" | "statuses";
  title: string;
  rows: Row[];
  onRun: (action: () => Promise<unknown>, message?: string) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [color, setColor] = useState(DEFAULT_COLOR);
  const base = `/interview-${kind}`;

  const add = () => {
    if (!name.trim()) return;
    void onRun(
      async () => {
        await api.post(base, { name: name.trim(), color });
        setName("");
        setColor(DEFAULT_COLOR);
      },
      t("common.saved"),
    );
  };

  const reorder = (row: Row, direction: -1 | 1) => {
    const index = rows.findIndex((item) => item.id === row.id);
    const target = rows[index + direction];
    if (!target) return;
    void onRun(async () => {
      await Promise.all([
        api.patch(`${base}/${row.id}`, { position: target.position }),
        api.patch(`${base}/${target.id}`, { position: row.position }),
      ]);
    });
  };

  return (
    <section className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h3>
      <ul className="divide-y divide-border rounded border border-border">
        {rows.length === 0 ? (
          <li className="px-3 py-2 text-xs text-muted-foreground">{t("interviews.noSteps")}</li>
        ) : null}
        {rows.map((row, index) => (
          <li key={row.id} className="flex flex-wrap items-center gap-2 px-2 py-1.5">
            <input
              type="color"
              className="h-6 w-8 cursor-pointer rounded border border-border bg-transparent p-0"
              value={row.color ?? DEFAULT_COLOR}
              onChange={(event) =>
                void onRun(() => api.patch(`${base}/${row.id}`, { color: event.target.value }))
              }
              aria-label={t("interviews.stepColor")}
            />
            <Input
              className="h-8 flex-1 text-sm"
              defaultValue={row.name}
              onBlur={(event) => {
                const value = event.target.value.trim();
                if (value && value !== row.name) {
                  void onRun(() => api.patch(`${base}/${row.id}`, { name: value }), t("common.saved"));
                }
              }}
            />
            <Button
              size="icon"
              variant="ghost"
              disabled={index === 0}
              onClick={() => reorder(row, -1)}
              aria-label={t("interviews.moveUp")}
            >
              <ArrowUp className="h-3.5 w-3.5" />
            </Button>
            <Button
              size="icon"
              variant="ghost"
              disabled={index === rows.length - 1}
              onClick={() => reorder(row, 1)}
              aria-label={t("interviews.moveDown")}
            >
              <ArrowDown className="h-3.5 w-3.5" />
            </Button>
            <Button
              size="icon"
              variant="ghost"
              onClick={() => void onRun(() => api.del(`${base}/${row.id}`))}
              aria-label={t("interviews.deleteStep")}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="color"
          className="h-8 w-9 cursor-pointer rounded border border-border bg-transparent p-0"
          value={color}
          onChange={(event) => setColor(event.target.value)}
          aria-label={t("interviews.stepColor")}
        />
        <Input
          className="h-8 w-48 text-sm"
          placeholder={kind === "steps" ? t("interviews.newStep") : t("interviews.newStatus")}
          value={name}
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") add();
          }}
        />
        <Button size="sm" variant="outline" onClick={add} disabled={!name.trim()}>
          <Plus className="h-3.5 w-3.5" />
          {t("common.add")}
        </Button>
      </div>
    </section>
  );
}
