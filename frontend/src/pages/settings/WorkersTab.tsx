import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Cpu, Gauge, MemoryStick, Zap } from "lucide-react";
import { api, errorMessage } from "../../lib/api";
import { Badge, Button, Card, CardHeader, ErrorNote, Field, Input, Select, Spinner } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import { t } from "../../i18n";
import type { PoolStatus, Settings } from "../../types";

/** The settings the panel writes; everything else on the page is untouched. */
const KEYS = [
  "llm_pool_mode",
  "llm_pool_static_size",
  "llm_pool_min",
  "llm_pool_max",
  "llm_grow_below_pct",
  "llm_admit_above_pct",
  "llm_shrink_above_pct",
  "llm_shrink_below_pct",
  "llm_scale_step",
  "llm_scale_interval_s",
  "llm_shrink_cooldown_s",
] as const;

const NUMBER_FIELDS: { key: (typeof KEYS)[number]; label: string; group: "size" | "advanced" }[] = [
  { key: "llm_pool_static_size", label: "settings.workers.size", group: "size" },
  { key: "llm_pool_min", label: "settings.workers.min", group: "size" },
  { key: "llm_pool_max", label: "settings.workers.max", group: "size" },
  { key: "llm_grow_below_pct", label: "settings.workers.growBelow", group: "advanced" },
  { key: "llm_admit_above_pct", label: "settings.workers.admitAbove", group: "advanced" },
  { key: "llm_shrink_above_pct", label: "settings.workers.shrinkAbove", group: "advanced" },
  { key: "llm_shrink_below_pct", label: "settings.workers.shrinkBelow", group: "advanced" },
  { key: "llm_scale_step", label: "settings.workers.step", group: "advanced" },
  { key: "llm_scale_interval_s", label: "settings.workers.interval", group: "advanced" },
  { key: "llm_shrink_cooldown_s", label: "settings.workers.cooldown", group: "advanced" },
];

/** CONC-2: static or dynamic generation processors, plus the live controller state. */
export function WorkersTab() {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Settings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [advanced, setAdvanced] = useState(false);

  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.get<{ values: Settings; schema: string[] }>("/settings"),
  });

  const pool = useQuery({
    queryKey: ["pool-status"],
    queryFn: () => api.get<PoolStatus>("/system/pool"),
    refetchInterval: 10_000,
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

  const mode = draft.llm_pool_mode === "dynamic" ? "dynamic" : "static";
  const state = pool.data?.state ?? { state: "unknown" };

  const save = async () => {
    setError(null);
    setSaving(true);
    try {
      const values: Record<string, unknown> = {};
      for (const key of KEYS) values[key] = draft[key];
      await api.patch("/settings", { values });
      // Apply immediately so the panel reflects the new mode without waiting
      // for the next controller tick (which runs every 10 s anyway).
      await api.post("/system/pool/apply", {});
      push({ tone: "success", title: t("settings.workers.saved") });
      await queryClient.invalidateQueries({ queryKey: ["settings"] });
      await queryClient.invalidateQueries({ queryKey: ["pool-status"] });
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setSaving(false);
    }
  };

  const applyNow = async () => {
    setSaving(true);
    setError(null);
    try {
      await api.post("/system/pool/apply", {});
      await queryClient.invalidateQueries({ queryKey: ["pool-status"] });
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-3">
      <Card>
        <CardHeader
          title={t("settings.workers.title")}
          description={t("settings.workers.intro")}
          actions={
            <>
              <Button size="sm" variant="outline" onClick={() => void applyNow()} loading={saving}>
                <Zap className="h-3.5 w-3.5" />
                {t("settings.workers.applyNow")}
              </Button>
              <Button size="sm" onClick={() => void save()} loading={saving}>
                {t("common.save")}
              </Button>
            </>
          }
        />
        <div className="grid gap-3 tablet:grid-cols-2">
          <Field label={t("settings.workers.mode")} hint={mode === "dynamic" ? t("settings.workers.dynamicHint") : t("settings.workers.staticHint")}>
            <Select
              value={mode}
              onChange={(event) => setDraft({ ...draft, llm_pool_mode: event.target.value as Settings["llm_pool_mode"] })}
            >
              <option value="static">{t("settings.workers.static")}</option>
              <option value="dynamic">{t("settings.workers.dynamic")}</option>
            </Select>
          </Field>
          {mode === "static" ? (
            <NumberField draft={draft} setDraft={setDraft} fieldKey="llm_pool_static_size" label={t("settings.workers.size")} />
          ) : (
            <>
              <NumberField draft={draft} setDraft={setDraft} fieldKey="llm_pool_min" label={t("settings.workers.min")} />
              <NumberField draft={draft} setDraft={setDraft} fieldKey="llm_pool_max" label={t("settings.workers.max")} hint={t("settings.workers.ceilingHint")} />
            </>
          )}
        </div>
        {mode === "dynamic" ? (
          <div className="mt-2">
            <Button size="sm" variant="ghost" onClick={() => setAdvanced((value) => !value)}>
              {advanced ? t("common.showLess") : t("common.showMore")} — {t("settings.workers.advanced")}
            </Button>
            {advanced ? (
              <div className="mt-2 grid gap-3 tablet:grid-cols-3">
                {NUMBER_FIELDS.filter((field) => field.group === "advanced").map((field) => (
                  <NumberField key={field.key} draft={draft} setDraft={setDraft} fieldKey={field.key} label={t(field.label)} />
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
        {error ? (
          <div className="mt-2">
            <ErrorNote>{error}</ErrorNote>
          </div>
        ) : null}
      </Card>

      <Card>
        <CardHeader
          title={t("settings.workers.live")}
          actions={<Badge tone={state.state === "shrinking" || state.state === "holding" ? "warning" : "success"}>{state.state}</Badge>}
        />
        {!pool.data ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner /> {t("common.loading")}
          </p>
        ) : (
          <>
            <div className="grid gap-3 tablet:grid-cols-3">
              <Stat
                icon={<Cpu className="h-3.5 w-3.5" />}
                label={t("settings.workers.liveProcessors")}
                value={String(state.size ?? state.ceiling ?? "—")}
                hint={t("settings.workers.liveRange", {
                  floor: state.floor ?? "—",
                  ceiling: state.ceiling ?? "—",
                })}
              />
              <Stat icon={<Gauge className="h-3.5 w-3.5" />} label={t("settings.workers.liveActive")} value={String(state.active ?? pool.data.active)} />
              <Stat icon={<Gauge className="h-3.5 w-3.5" />} label={t("settings.workers.liveQueued")} value={String(pool.data.backlog)} />
              <Stat
                icon={<MemoryStick className="h-3.5 w-3.5" />}
                label={t("settings.workers.liveMemory")}
                value={state.memory_used_pct === undefined ? "—" : `${state.memory_used_pct}%`}
                hint={
                  state.memory_total_mb
                    ? t("settings.workers.memoryHint", {
                        used: state.memory_used_pct ?? "—",
                        available: state.memory_available_mb ?? "—",
                        total: state.memory_total_mb,
                      })
                    : undefined
                }
              />
              <Stat icon={<Cpu className="h-3.5 w-3.5" />} label={t("settings.workers.liveCpu")} value={state.cpu_pct === null || state.cpu_pct === undefined ? "—" : `${state.cpu_pct}%`} />
              <Stat icon={<Gauge className="h-3.5 w-3.5" />} label={t("settings.workers.liveMode")} value={String(state.mode ?? mode)} />
            </div>
            <p className="mt-3 text-xs text-muted-foreground">{state.reason ?? t("settings.workers.noTick")}</p>
          </>
        )}
      </Card>
    </div>
  );
}

function NumberField({
  draft,
  setDraft,
  fieldKey,
  label,
  hint,
}: {
  draft: Settings;
  setDraft: (value: Settings) => void;
  fieldKey: (typeof KEYS)[number];
  label: string;
  hint?: string;
}) {
  return (
    <Field label={label} hint={hint}>
      <Input
        type="number"
        value={String(draft[fieldKey] ?? "")}
        onChange={(event) => setDraft({ ...draft, [fieldKey]: Number(event.target.value) })}
      />
    </Field>
  );
}

function Stat({ icon, label, value, hint }: { icon: React.ReactNode; label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-md border border-border px-3 py-2">
      <p className="flex items-center gap-1 text-xs text-muted-foreground">
        {icon}
        {label}
      </p>
      <p className="text-lg font-semibold">{value}</p>
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  );
}
