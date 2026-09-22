import { useQuery } from "@tanstack/react-query";
import { PauseCircle, PlayCircle } from "lucide-react";
import { api, errorMessage } from "../../lib/api";
import { Badge, Button, Card, CardHeader, ErrorNote, Spinner } from "../../ui/primitives";
import { Table } from "../../ui/table";
import { useToast } from "../../ui/toast";
import { formatBytes, formatDateTime, formatDuration } from "../../lib/format";
import { t } from "../../i18n";
import type { SystemStatus } from "../../types";

/** SET-14: the live system card. */
export function SystemStatusTab() {
  const { push } = useToast();
  const status = useQuery({
    queryKey: ["system-status"],
    queryFn: () => api.get<SystemStatus>("/system/status"),
    refetchInterval: 10_000,
  });

  if (status.isLoading) {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <Spinner /> {t("common.loading")}
      </p>
    );
  }
  if (status.isError || !status.data) return <ErrorNote>{errorMessage(status.error)}</ErrorNote>;
  const data = status.data;
  const oldest = data.oldest_queued_at ? (Date.now() - new Date(data.oldest_queued_at).getTime()) / 1000 : null;

  const toggleIntake = async () => {
    try {
      await api.post(data.disk.intake_paused ? "/system/intake/resume" : "/system/intake/pause");
      await status.refetch();
    } catch (error) {
      push({ tone: "error", title: errorMessage(error) });
    }
  };

  return (
    <div className="grid gap-3 tablet:grid-cols-2">
      <Card>
        <CardHeader title={t("settings.status.queueDepth")} />
        <dl className="grid grid-cols-2 gap-2 text-sm">
          <div>
            <dt className="rf-label">llm</dt>
            <dd className="text-lg font-semibold">{data.queues.llm}</dd>
          </div>
          <div>
            <dt className="rf-label">render</dt>
            <dd className="text-lg font-semibold">{data.queues.render}</dd>
          </div>
          <div>
            <dt className="rf-label">{t("settings.status.inFlight")}</dt>
            <dd className="text-lg font-semibold">{data.in_flight_llm}</dd>
          </div>
          <div>
            <dt className="rf-label">{t("settings.status.oldestQueued")}</dt>
            <dd className="text-lg font-semibold">{oldest === null ? "—" : formatDuration(oldest)}</dd>
          </div>
        </dl>
      </Card>

      <Card>
        <CardHeader title={t("settings.status.leases")} />
        <p className="text-sm">
          {data.active_leases} active · {data.expired_leases_last_hour} {t("settings.status.leasesExpired")}
        </p>
        <p className="mt-2 text-sm">
          {t("settings.status.outbox")}: {data.outbox_backlog} {t("settings.status.backlog")}
        </p>
        <p className="mt-2 text-sm">
          {t("settings.status.sweeps")}: {data.sweeps_24h}
        </p>
        <p className="mt-2 text-sm">
          {t("settings.status.retries")}: llm {data.retries_last_hour.llm ?? 0} · render {data.retries_last_hour.render ?? 0}
        </p>
      </Card>

      <Card>
        <CardHeader title={t("settings.status.disk")} />
        <p className="text-sm">
          {data.disk.usage_pct.toFixed(1)}% · {data.disk.storage_dir}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          warn {data.disk.warn_pct}% · pause intake {data.disk.pause_intake_pct}% · pause render {data.disk.pause_render_pct}%
        </p>
        <p className="mt-2 text-sm">
          {t("settings.status.database")}: {data.database_size_bytes === null ? "—" : formatBytes(data.database_size_bytes)}
        </p>
        <div className="mt-3 flex items-center gap-2">
          {data.disk.intake_paused ? (
            <Badge tone="danger">
              <PauseCircle className="h-3 w-3" /> {t("settings.status.intakePaused")}
            </Badge>
          ) : null}
          <Button size="sm" variant="outline" onClick={() => void toggleIntake()}>
            <PlayCircle className="h-3.5 w-3.5" />
            {data.disk.intake_paused ? t("settings.status.resumeIntake") : t("settings.status.pauseIntake")}
          </Button>
        </div>
      </Card>

      <Card>
        <CardHeader title={t("settings.status.unoserver")} />
        <p className="text-sm">
          {data.unoserver.available ? t("common.active") : t("common.disabled")} · {data.unoserver.conversions} {t("settings.status.conversions")}
        </p>
        <div className="mt-3 space-y-1 text-xs text-muted-foreground">
          <p>
            app {data.versions.app} · generator {data.versions.generator}
          </p>
          <p>LibreOffice: {data.versions.libreoffice ?? "—"}</p>
        </div>
      </Card>

      <Card className="tablet:col-span-2">
        <CardHeader title={t("settings.status.workers")} />
        <Table>
          <thead>
            <tr>
              <th>{t("common.name")}</th>
              <th>Queues</th>
              <th>{t("settings.status.inFlight")}</th>
              <th>{t("common.status")}</th>
              <th>Heartbeat</th>
            </tr>
          </thead>
          <tbody>
            {data.workers.map((worker) => (
              <tr key={worker.name}>
                <td className="font-mono text-xs">{worker.name}</td>
                <td className="text-xs">{(worker.queues ?? []).join(", ")}</td>
                <td>{worker.concurrency ?? "—"}</td>
                <td>
                  <Badge tone={worker.alive ? "success" : "danger"}>{worker.alive ? t("common.active") : "stale"}</Badge>
                </td>
                <td className="text-xs">{worker.last_heartbeat_at ? formatDateTime(worker.last_heartbeat_at) : "—"}</td>
              </tr>
            ))}
            {data.workers.length === 0 ? (
              <tr>
                <td colSpan={5} className="py-4 text-center text-sm text-muted-foreground">
                  {t("common.noResults")}
                </td>
              </tr>
            ) : null}
          </tbody>
        </Table>
      </Card>

      <Card className="tablet:col-span-2">
        <CardHeader title={t("settings.status.needsAttention")} />
        {data.needs_attention_total === 0 ? (
          <p className="text-sm text-muted-foreground">{t("settings.status.noAttention")}</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {Object.entries(data.needs_attention).map(([kind, count]) => (
              <li key={kind} className="flex items-center gap-2">
                <Badge tone="danger">{count}</Badge>
                <span>{kind === "regenerate" ? t("settings.status.regeneration") : t("settings.status.blocking")}</span>
                <a className="text-xs underline" href={`/resumes?attention=${kind === "regenerate" ? "regeneration" : "blocking"}`}>
                  {t("resumes.title")}
                </a>
              </li>
            ))}
          </ul>
        )}
        {data.retention.expired_generations ? (
          <p className="mt-2 text-xs text-muted-foreground">
            retention: {data.retention.expired_generations} generations · {formatBytes(data.retention.bytes_freed)}
          </p>
        ) : null}
      </Card>
    </div>
  );
}
