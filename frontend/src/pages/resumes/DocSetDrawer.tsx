import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Copy } from "lucide-react";
import { api, errorMessage } from "../../lib/api";
import { formatBytes, formatDateTime, formatMs, formatSeq, fileDownloadUrl, downloadUrl, openPreview } from "../../lib/format";
import { Badge, Button, Card, CardHeader, Spinner } from "../../ui/primitives";
import { Drawer } from "../../ui/dialog";
import { Table } from "../../ui/table";
import { useToast } from "../../ui/toast";
import { StatusChip } from "../../components/StatusChip";
import { FileChips } from "../../components/FileChips";
import { t } from "../../i18n";
import type { DocSetRow, Generation, JobRow } from "../../types";

interface DetailPayload {
  doc_set: Record<string, unknown> & { id: string; company_name: string | null; job_title: string | null; storage_dir: string | null };
  job: JobRow;
  generations: Generation[];
  interviews: { id: string; generation_id: string; status: string; meeting_at: string | null }[];
  jd_text?: string;
  timeline?: { from_state: string | null; to_state: string; stage: string | null; attempt_no: number | null; actor: string | null; details: Record<string, unknown>; at: string }[];
  llm_json?: Record<string, unknown> | null;
}

export function DocSetDrawer({
  row,
  onClose,
  role,
  onCancelled,
}: {
  row: DocSetRow | null;
  onClose: () => void;
  role: string;
  onCancelled: () => void;
}) {
  const { push } = useToast();
  const [tab, setTab] = useState<"jd" | "generations" | "timeline" | "llm">("generations");

  const detail = useQuery({
    queryKey: ["doc-set", row?.doc_set_id],
    queryFn: () => api.get<DetailPayload>(`/doc-sets/${row?.doc_set_id}`),
    enabled: Boolean(row?.doc_set_id),
  });

  if (!row) return null;
  const isManager = role === "manager";
  const data = detail.data;

  return (
    <Drawer
      open={Boolean(row)}
      onClose={onClose}
      title={
        <span className="flex items-center gap-2">
          {t("resumes.detail.title", { seq: formatSeq(row.seq_no) })} · {row.doc_set?.company_name ?? t("common.unknown")}
          <StatusChip status={row.status} role={role as "manager"} />
        </span>
      }
    >
      {detail.isLoading ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> {t("common.loading")}
        </p>
      ) : detail.isError ? (
        <p className="text-sm text-destructive">{errorMessage(detail.error)}</p>
      ) : data ? (
        <div className="space-y-4">
          <div className="flex flex-wrap gap-2">
            {(["generations", isManager ? "jd" : null, isManager ? "timeline" : null, isManager ? "llm" : null] as const)
              .filter((value): value is "generations" | "jd" | "timeline" | "llm" => Boolean(value))
              .map((value) => (
                <Button key={value} size="sm" variant={tab === value ? "primary" : "outline"} onClick={() => setTab(value)}>
                  {value === "generations"
                    ? t("resumes.detail.generations")
                    : value === "jd"
                      ? t("resumes.detail.jd")
                      : value === "timeline"
                        ? t("resumes.detail.timeline")
                        : t("resumes.detail.llmJson")}
                </Button>
              ))}
          </div>

          {tab === "generations" ? (
            <div className="space-y-3">
              {data.generations.map((generation) => {
                const pinned = data.interviews.filter((interview) => interview.generation_id === generation.id);
                const isCurrent = generation.id === row.doc_set?.current_generation_id;
                return (
                  <Card key={generation.id}>
                    <CardHeader
                      title={
                        <span className="flex flex-wrap items-center gap-2">
                          gen {generation.generation_no}
                          <Badge tone="outline">{generation.kind}</Badge>
                          <Badge tone={generation.status === "ready" ? "success" : generation.status === "needs_attention" ? "danger" : "info"}>
                            {generation.status}
                          </Badge>
                          {isCurrent ? <Badge tone="info">{t("resumes.detail.current")}</Badge> : null}
                          {pinned.length ? <Badge tone="warning">{t("resumes.detail.pinnedBy", { count: pinned.length })}</Badge> : null}
                        </span>
                      }
                      description={
                        <span>
                          {formatDateTime(generation.created_at)}
                          {generation.expired_at ? ` · ${t("resumes.detail.expires", { date: formatDateTime(generation.expired_at) })}` : ""}
                        </span>
                      }
                      actions={
                        generation.kind === "regenerate" &&
                        ["queued", "llm_running", "rendering", "retry_wait"].includes(generation.status) ? (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={async () => {
                              try {
                                await api.post(`/generations/${generation.id}/cancel`);
                                push({ tone: "success", title: t("toast.updated") });
                                onCancelled();
                              } catch (error) {
                                push({ tone: "error", title: errorMessage(error) });
                              }
                            }}
                          >
                            {t("resumes.cancelGeneration")}
                          </Button>
                        ) : null
                      }
                    />
                    <FileChips files={generation.files} />
                    {generation.last_error_message ? (
                      <p className="mt-2 text-xs text-destructive">{generation.last_error_message}</p>
                    ) : null}
                    {isManager ? (
                      <div className="mt-3 grid gap-2 text-xs text-muted-foreground tablet:grid-cols-2">
                        <span>
                          {t("resumes.detail.provider")}: {generation.snapshot.provider_id ?? "—"} · {t("resumes.detail.model")}: {generation.snapshot.model ?? "—"}
                        </span>
                        <span>
                          {t("resumes.detail.promptVersion")}: {generation.snapshot.prompt_version_id?.slice(0, 8) ?? "—"} · {t("resumes.detail.theme")}:{" "}
                          {String((generation.snapshot.theme_snapshot as { name?: string } | null)?.name ?? generation.snapshot.theme_id ?? "—")}
                        </span>
                        <span>
                          {t("resumes.detail.lease")}: {generation.claimed_by ?? "—"} · {generation.lease_expires_at ? formatDateTime(generation.lease_expires_at) : "—"}
                        </span>
                        <span>
                          attempts: llm {generation.llm_attempts} · render {generation.render_attempts}
                        </span>
                      </div>
                    ) : null}
                    {isManager && generation.attempts.length ? (
                      <div className="mt-3">
                        <Table>
                          <thead>
                            <tr>
                              <th>{t("common.status")}</th>
                              <th>#</th>
                              <th>{t("resumes.detail.worker")}</th>
                              <th>{t("common.created")}</th>
                              <th>ms</th>
                              <th>tokens</th>
                              <th>{t("common.error")}</th>
                            </tr>
                          </thead>
                          <tbody>
                            {generation.attempts.map((attempt) => (
                              <tr key={attempt.id}>
                                <td>
                                  {attempt.stage} · {attempt.outcome ?? "—"}
                                </td>
                                <td>{attempt.attempt_no}</td>
                                <td className="font-mono text-xs">{attempt.worker ?? "—"}</td>
                                <td>{formatDateTime(attempt.started_at)}</td>
                                <td>{formatMs(attempt.latency_ms)}</td>
                                <td>
                                  {attempt.tokens_in ?? 0}/{attempt.tokens_out ?? 0}
                                  {attempt.tokens_cached ? ` (${attempt.tokens_cached} cached)` : ""}
                                </td>
                                <td className="text-destructive">{attempt.error_code ?? ""}</td>
                              </tr>
                            ))}
                          </tbody>
                        </Table>
                      </div>
                    ) : null}
                  </Card>
                );
              })}
            </div>
          ) : null}

          {tab === "jd" && isManager ? (
            <Card>
              <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap font-mono text-xs">{data.jd_text ?? ""}</pre>
            </Card>
          ) : null}

          {tab === "timeline" && isManager ? (
            <Card>
              <ul className="space-y-2 text-xs">
                {(data.timeline ?? []).map((event, index) => (
                  <li key={index} className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-muted-foreground">{formatDateTime(event.at)}</span>
                    <Badge tone="outline">{event.stage ?? "job"}</Badge>
                    <span>
                      {event.from_state ? `${event.from_state} → ` : ""}
                      <strong>{event.to_state}</strong>
                    </span>
                    <span className="text-muted-foreground">{event.actor}</span>
                  </li>
                ))}
                {(data.timeline ?? []).length === 0 ? <li className="text-muted-foreground">{t("common.noResults")}</li> : null}
              </ul>
            </Card>
          ) : null}

          {tab === "llm" && isManager ? (
            <Card>
              {data.llm_json ? (
                <div className="space-y-2">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      void navigator.clipboard.writeText(JSON.stringify(data.llm_json, null, 2));
                      push({ tone: "success", title: t("common.copied") });
                    }}
                  >
                    <Copy className="h-3.5 w-3.5" />
                    {t("resumes.detail.copyJson")}
                  </Button>
                  <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap font-mono text-xs">
                    {JSON.stringify(data.llm_json, null, 2)}
                  </pre>
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">{t("resumes.detail.noLlmJson")}</p>
              )}
            </Card>
          ) : null}

          <div className="flex flex-wrap gap-2">
            {(["pdf", "docx", "txt"] as const).map((kind) => {
              const file = (data.generations.find((generation) => generation.id === row.doc_set?.current_generation_id)?.files ?? []).find(
                (candidate) => candidate.kind === kind,
              );
              if (!file) return null;
              return (
                <span key={kind} className="flex items-center gap-1">
                  <Button size="sm" variant="outline" onClick={() => downloadUrl(fileDownloadUrl(file.id))}>
                    {kind.toUpperCase()} · {formatBytes(file.size_bytes)}
                  </Button>
                  {kind !== "docx" ? (
                    <Button size="sm" variant="ghost" onClick={() => openPreview(fileDownloadUrl(file.id, true))}>
                      {t("common.preview")}
                    </Button>
                  ) : null}
                </span>
              );
            })}
          </div>
        </div>
      ) : null}
    </Drawer>
  );
}
