import { useQuery } from "@tanstack/react-query";
import { Activity, AlertTriangle, PauseCircle } from "lucide-react";
import { api } from "../../lib/api";
import { formatDuration } from "../../lib/format";
import { Badge, Button, Card } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import { t } from "../../i18n";
import type { SystemStatus } from "../../types";

/** RES-19: needs-attention banner + "Processing now" strip for Managers. */
export function ProcessingStrip({ onFilterAttention, onFilterRegeneration }: { onFilterAttention: (kind: "blocking" | "regeneration") => void; onFilterRegeneration: () => void }) {
  const { push } = useToast();
  const status = useQuery({
    queryKey: ["system-status"],
    queryFn: () => api.get<SystemStatus>("/system/status"),
    refetchInterval: 15_000,
  });

  const data = status.data;
  if (!data) return null;

  const attentionItems = Object.entries(data.needs_attention ?? {});
  const blocking = attentionItems
    .filter(([kind]) => kind !== "regenerate")
    .reduce((total, [, count]) => total + count, 0);
  const regenerations = data.needs_attention?.regenerate ?? 0;
  const oldestAge = data.oldest_queued_at
    ? (Date.now() - new Date(data.oldest_queued_at).getTime()) / 1000
    : null;

  const toggleIntake = async () => {
    try {
      await api.post(data.disk.intake_paused ? "/system/intake/resume" : "/system/intake/pause");
      push({ tone: "success", title: t("toast.updated") });
      await status.refetch();
    } catch (error) {
      push({ tone: "error", title: error instanceof Error ? error.message : t("toast.failed") });
    }
  };

  return (
    <div className="space-y-2">
      {data.needs_attention_total > 0 ? (
        <div className="flex flex-wrap items-center gap-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          <AlertTriangle className="h-4 w-4" />
          <span>{t("resumes.needsAttention", { count: blocking, makers: data.workers.length ? "—" : "0", regenerations })}</span>
          {blocking > 0 ? (
            <Button size="sm" variant="outline" onClick={() => onFilterAttention("blocking")}>
              {t("resumes.attentionBlocking")}
            </Button>
          ) : null}
          {regenerations > 0 ? (
            <Button size="sm" variant="outline" onClick={onFilterRegeneration}>
              {t("resumes.attentionRegeneration")}
            </Button>
          ) : null}
        </div>
      ) : null}

      <Card className="flex flex-wrap items-center gap-4 text-xs">
        <span className="flex items-center gap-1.5 font-medium">
          <Activity className="h-3.5 w-3.5 text-muted-foreground" />
          {t("resumes.processingNow")}
        </span>
        <span>
          {t("resumes.queue")}: llm <strong>{data.queues.llm}</strong> · render <strong>{data.queues.render}</strong>
        </span>
        <span>
          {t("resumes.inFlight")}: <strong>{data.in_flight_llm}</strong>
        </span>
        <span>
          {t("resumes.oldestWaiting")}: <strong>{oldestAge === null ? "—" : formatDuration(oldestAge)}</strong>
        </span>
        <span>
          {t("settings.status.disk")}: <strong>{data.disk.usage_pct.toFixed(1)}%</strong> ({data.disk.storage_dir})
        </span>
        {data.disk.intake_paused ? (
          <Badge tone="danger">
            <PauseCircle className="h-3 w-3" /> {t("resumes.intakePaused")}
          </Badge>
        ) : null}
        <Button size="sm" variant={data.disk.intake_paused ? "primary" : "outline"} className="ml-auto" onClick={() => void toggleIntake()}>
          {data.disk.intake_paused ? t("settings.status.resumeIntake") : t("settings.status.pauseIntake")}
        </Button>
      </Card>
    </div>
  );
}
