import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, errorMessage } from "../../lib/api";
import { formatDuration } from "../../lib/format";
import { Card, CardHeader, Spinner } from "../../ui/primitives";
import { Table } from "../../ui/table";
import { t } from "../../i18n";

interface MakerStat {
  maker_id: string;
  maker_name: string;
  maker_email: string;
  submitted: number;
  ready: number;
  skipped: number;
  selected: number;
  tokens: number;
  avg_ready_ms: number | null;
  avg_llm_attempts: number | null;
  success_rate: number | null;
}

/** RES-16: per-maker stats bar for the chosen date range. */
export function MakerStats({
  from,
  to,
  onPick,
}: {
  from: string;
  to: string;
  onPick: (makerId: string) => void;
}) {
  const [sortKey, setSortKey] = useState<keyof MakerStat>("submitted");
  const [dir, setDir] = useState<"asc" | "desc">("desc");

  const stats = useQuery({
    queryKey: ["stats", "makers", { from, to }],
    queryFn: () => api.get<{ items: MakerStat[] }>("/stats/makers", { from, to }),
  });

  const items = [...(stats.data?.items ?? [])].sort((a, b) => {
    const left = a[sortKey] ?? 0;
    const right = b[sortKey] ?? 0;
    if (left === right) return 0;
    return (left > right ? 1 : -1) * (dir === "asc" ? 1 : -1);
  });

  const totals = items.reduce(
    (acc, item) => ({
      submitted: acc.submitted + item.submitted,
      ready: acc.ready + item.ready,
      skipped: acc.skipped + item.skipped,
      selected: acc.selected + item.selected,
      tokens: acc.tokens + item.tokens,
    }),
    { submitted: 0, ready: 0, skipped: 0, selected: 0, tokens: 0 },
  );
  const success = totals.ready ? Math.round((totals.selected / totals.ready) * 1000) / 10 : null;

  const header = (key: keyof MakerStat, label: string) => (
    <th
      className="cursor-pointer"
      onClick={() => {
        if (sortKey === key) setDir(dir === "asc" ? "desc" : "asc");
        else {
          setSortKey(key);
          setDir("desc");
        }
      }}
    >
      {label}
      {sortKey === key ? (dir === "asc" ? " ↑" : " ↓") : ""}
    </th>
  );

  return (
    <Card>
      <CardHeader
        title={t("resumes.statsTitle")}
        description={`${from} → ${to} · ${t("resumes.stats.submitted")} ${totals.submitted} · ${t("resumes.stats.ready")} ${totals.ready} · ${t("resumes.stats.skipped")} ${totals.skipped} · ${t("resumes.stats.selected")} ${totals.selected} · ${t("resumes.stats.success")} ${success === null ? "—" : `${success}%`}`}
      />
      {stats.isLoading ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> {t("common.loading")}
        </p>
      ) : stats.isError ? (
        <p className="text-sm text-destructive">{errorMessage(stats.error)}</p>
      ) : items.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("common.noResults")}</p>
      ) : (
        <Table>
          <thead>
            <tr>
              <th>{t("resumes.columns.maker")}</th>
              {header("submitted", t("resumes.stats.submitted"))}
              {header("ready", t("resumes.stats.ready"))}
              {header("skipped", t("resumes.stats.skipped"))}
              {header("selected", t("resumes.stats.selected"))}
              {header("success_rate", t("resumes.stats.success"))}
              {header("avg_ready_ms", t("resumes.stats.avgReady"))}
              {header("avg_llm_attempts", t("resumes.stats.avgAttempts"))}
              {header("tokens", t("resumes.stats.tokens"))}
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.maker_id} className="cursor-pointer hover:bg-accent/40" onClick={() => onPick(item.maker_id)}>
                <td className="font-medium">{item.maker_name || item.maker_email}</td>
                <td>{item.submitted}</td>
                <td>{item.ready}</td>
                <td>{item.skipped}</td>
                <td>{item.selected}</td>
                <td>{item.success_rate === null ? "—" : `${item.success_rate}%`}</td>
                <td>{item.avg_ready_ms === null ? "—" : formatDuration(item.avg_ready_ms / 1000)}</td>
                <td>{item.avg_llm_attempts === null ? "—" : item.avg_llm_attempts.toFixed(1)}</td>
                <td>{item.tokens.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}
