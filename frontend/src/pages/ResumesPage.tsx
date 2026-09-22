import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUp, BarChart3, Download, FileDown, Star } from "lucide-react";
import { api, downloadBlob, errorMessage } from "../lib/api";
import { useAuth } from "../auth/AuthProvider";
import { t } from "../i18n";
import { Badge, Button, Card, Checkbox, EmptyState, Input, Select, Spinner } from "../ui/primitives";
import { Pager } from "../ui/table";
import { useToast } from "../ui/toast";
import { DateSelector } from "../components/DateSelector";
import { ResumesTable } from "./resumes/ResumesTable";
import { DocSetDrawer } from "./resumes/DocSetDrawer";
import { MakerStats } from "./resumes/MakerStats";
import { ProcessingStrip } from "./resumes/ProcessingStrip";
import { useUrlState } from "../lib/useUrlState";
import { useRealtime } from "../realtime/RealtimeProvider";
import { downloadText, toCsv } from "../lib/utils";
import { todayISO } from "../lib/format";
import type { DocSetRow, Paginated, Profile, User } from "../types";

const DEFAULTS = {
  date: todayISO(),
  date_from: "",
  date_to: "",
  status: "all",
  q: "",
  maker_id: [] as string[],
  profile_id: "",
  selected: false,
  duplicates: false,
  multi_generation: false,
  kept: false,
  attention: "",
  sort: "seq",
  order: "asc",
  page: 1,
};

const STATUS_CHIPS = ["all", "ready", "processing", "retrying", "attention", "skipped", "expired", "selected"] as const;

export function ResumesPage() {
  const { user } = useAuth();
  const { push } = useToast();
  const queryClient = useQueryClient();
  const { state, update } = useUrlState(DEFAULTS);
  const { subscribe } = useRealtime();
  // RES-8: count live events that arrive while the list is scrolled away from the top.
  const [newRows, setNewRows] = useState(0);
  const [scrolledAway, setScrolledAway] = useState(false);
  const [selection, setSelection] = useState<string[]>([]);
  const [openRow, setOpenRow] = useState<DocSetRow | null>(null);
  const [showStats, setShowStats] = useState(false);
  const isManager = user?.role === "manager";
  const rangeActive = Boolean(state.date_from && state.date_to);

  const query = useMemo(
    () => ({
      date: rangeActive ? undefined : state.date || todayISO(),
      date_from: rangeActive ? state.date_from : undefined,
      date_to: rangeActive ? state.date_to : undefined,
      status: state.status === "all" ? undefined : state.status,
      q: state.q || undefined,
      maker_id: state.maker_id.length ? state.maker_id : undefined,
      profile_id: state.profile_id || undefined,
      selected: state.selected || undefined,
      duplicates: state.duplicates || undefined,
      multi_generation: state.multi_generation || undefined,
      kept: state.kept || undefined,
      attention: state.attention || undefined,
      sort: state.sort,
      order: state.order,
      page: state.page,
      page_size: 50,
    }),
    [state, rangeActive],
  );

  const docSets = useQuery({
    queryKey: ["doc-sets", query],
    queryFn: () => api.get<Paginated<DocSetRow>>("/doc-sets", query as Record<string, string | number | boolean | undefined>),
  });

  const makers = useQuery({
    queryKey: ["users", { role: "maker" }],
    queryFn: () => api.get<Paginated<User>>("/users", { role: "maker", page_size: 200 }),
    enabled: isManager,
  });

  const profiles = useQuery({
    queryKey: ["profiles"],
    queryFn: () => api.get<{ items: Profile[] }>("/profiles"),
    enabled: isManager,
  });

  const rows = docSets.data?.items ?? [];

  useEffect(() => {
    const onScroll = () => setScrolledAway(window.scrollY > 160);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(
    () =>
      subscribe((event) => {
        if (!["job.status", "job.released", "build.needs_attention", "docset.regenerated", "docset.selected"].includes(event.type)) return;
        if (window.scrollY > 160) setNewRows((count) => count + 1);
      }),
    [subscribe],
  );

  useEffect(() => {
    if (!scrolledAway && newRows) setNewRows(0);
  }, [scrolledAway, newRows]);
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["doc-sets"] });

  const downloadSelection = async () => {
    if (!selection.length) return;
    try {
      const blob = await api.requestBlob("/doc-sets/zip", { method: "POST", body: { ids: selection } });
      await downloadBlob(blob);
      push({ tone: "success", title: t("toast.downloaded") });
    } catch (error) {
      push({ tone: "error", title: errorMessage(error) });
    }
  };

  const downloadDate = async () => {
    try {
      const blob = await api.requestBlob("/doc-sets/zip", {
        query: {
          date: rangeActive ? undefined : state.date || todayISO(),
          date_from: rangeActive ? state.date_from : undefined,
          date_to: rangeActive ? state.date_to : undefined,
          maker_id: state.maker_id.length === 1 ? state.maker_id[0] : undefined,
        },
      });
      await downloadBlob(blob);
    } catch (error) {
      push({ tone: "error", title: errorMessage(error) });
    }
  };

  const bulkSelect = async (selected: boolean) => {
    if (!selection.length) return;
    try {
      await api.post("/doc-sets/bulk-select", { ids: selection, selected });
      push({ tone: "success", title: selected ? t("toast.selected", { count: selection.length }) : t("toast.unselected", { count: selection.length }) });
      setSelection([]);
      refresh();
    } catch (error) {
      push({ tone: "error", title: errorMessage(error) });
    }
  };

  const exportCsv = () => {
    const csv = toCsv(
      rows.map((row) => ({
        seq: row.seq_no,
        maker: row.maker_name ?? "",
        date: row.submitted_date,
        company: row.doc_set?.company_name ?? "",
        title: row.doc_set?.job_title ?? "",
        status: row.status?.label ?? "",
        attempts: row.attempts ?? "",
        generations: row.generation_count,
        selected: row.is_selected,
        submitted_at: row.submitted_at ?? "",
        released_at: row.released_at ?? "",
        tokens: row.tokens ?? "",
      })),
      ["seq", "maker", "date", "company", "title", "status", "attempts", "generations", "selected", "submitted_at", "released_at", "tokens"],
    );
    downloadText(`resumes_${state.date || todayISO()}.csv`, csv);
  };

  const toggleMaker = (makerId: string) => {
    update({
      maker_id: state.maker_id.includes(makerId) ? state.maker_id.filter((value) => value !== makerId) : [...state.maker_id, makerId],
      page: 1,
    });
  };

  return (
    <div className="space-y-4">
      {newRows > 0 ? (
        <div className="sticky top-3 z-30 flex justify-center">
          <button
            type="button"
            className="flex items-center gap-1.5 rounded-full border border-border bg-card px-3 py-1 text-xs shadow-sm hover:bg-accent"
            onClick={() => {
              window.scrollTo({ top: 0, behavior: "smooth" });
              setNewRows(0);
            }}
          >
            <ArrowUp className="h-3.5 w-3.5" />
            {t("resumes.newRows", { count: newRows })}
          </button>
        </div>
      ) : null}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{t("resumes.title")}</h1>
          <p className="text-sm text-muted-foreground">
            {state.date || todayISO()}
            {state.maker_id.length ? ` · ${state.maker_id.length} maker(s)` : ""}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" onClick={exportCsv} disabled={!rows.length}>
            <FileDown className="h-3.5 w-3.5" />
            {t("resumes.exportCsv")}
          </Button>
          <Button variant="outline" size="sm" onClick={downloadDate}>
            <Download className="h-3.5 w-3.5" />
            {t("resumes.downloadDate")}
          </Button>
          {isManager ? (
            <Button variant="outline" size="sm" onClick={() => setShowStats((value) => !value)}>
              <BarChart3 className="h-3.5 w-3.5" />
              {t("resumes.statsTitle")}
            </Button>
          ) : null}
        </div>
      </div>

      {isManager ? (
        <ProcessingStrip
          onFilterAttention={(kind) => update({ attention: kind, page: 1 })}
          onFilterRegeneration={() => update({ attention: "regeneration", page: 1 })}
        />
      ) : null}

      <Card className="space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <DateSelector
            date={state.date || todayISO()}
            onDate={(value) => update({ date: value, date_from: "", date_to: "", page: 1 })}
            range={{ from: state.date_from, to: state.date_to }}
            onRange={(value) => update({ date_from: value.from, date_to: value.to, page: 1 })}
            withRange={isManager}
          />
          <Input
            className="h-8 w-full max-w-xs text-sm"
            placeholder={t("resumes.searchPlaceholder")}
            value={state.q}
            onChange={(event) => update({ q: event.target.value, page: 1 })}
          />
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {STATUS_CHIPS.map((chip) => (
            <button
              key={chip}
              type="button"
              onClick={() => update({ status: chip, page: 1 })}
              className={
                "rounded-full border px-2.5 py-1 text-xs transition " +
                (state.status === chip ? "border-primary bg-primary/10 text-primary" : "border-border hover:bg-accent")
              }
            >
              {t(`resumes.filterStatus.${chip}`)}
            </button>
          ))}
        </div>

        {isManager ? (
          <div className="flex flex-wrap items-center gap-3 text-xs">
            <Select
              className="h-8 w-40 text-xs"
              value={state.profile_id}
              onChange={(event) => update({ profile_id: event.target.value, page: 1 })}
            >
              <option value="">{t("profiles.title")}: {t("common.all")}</option>
              {(profiles.data?.items ?? []).map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.name}
                </option>
              ))}
            </Select>
            <span className="flex items-center gap-2">
              {(makers.data?.items ?? []).slice(0, 6).map((maker) => (
                <label key={maker.id} className="inline-flex cursor-pointer items-center gap-1">
                  <input
                    type="checkbox"
                    className="h-3.5 w-3.5 accent-[hsl(var(--primary))]"
                    checked={state.maker_id.includes(maker.id)}
                    onChange={() => toggleMaker(maker.id)}
                  />
                  {maker.name}
                </label>
              ))}
            </span>
            <Checkbox label={t("resumes.filterStatus.selected")} checked={state.selected} onChange={(event) => update({ selected: event.target.checked, page: 1 })} />
            <Checkbox label="Duplicates" checked={state.duplicates} onChange={(event) => update({ duplicates: event.target.checked, page: 1 })} />
            <Checkbox label="Multi-gen" checked={state.multi_generation} onChange={(event) => update({ multi_generation: event.target.checked, page: 1 })} />
            <Checkbox label={t("resumes.keep")} checked={state.kept} onChange={(event) => update({ kept: event.target.checked, page: 1 })} />
            <Checkbox label={t("resumes.attentionFilter")} checked={Boolean(state.attention)} onChange={(event) => update({ attention: event.target.checked ? "blocking" : "", page: 1 })} />
          </div>
        ) : null}

        {selection.length ? (
          <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-muted/50 px-3 py-2 text-xs">
            <Badge tone="info">{t("common.rowsSelected", { count: selection.length })}</Badge>
            <Button size="sm" variant="outline" onClick={() => void downloadSelection()}>
              {t("resumes.downloadSelected", { count: selection.length })}
            </Button>
            {isManager ? (
              <>
                <Button size="sm" variant="outline" onClick={() => void bulkSelect(true)}>
                  <Star className="h-3.5 w-3.5" />
                  {t("resumes.bulkSelect")}
                </Button>
                <Button size="sm" variant="outline" onClick={() => void bulkSelect(false)}>
                  {t("resumes.bulkUnselect")}
                </Button>
              </>
            ) : null}
            <Button size="sm" variant="ghost" onClick={() => setSelection([])}>
              {t("common.clear")}
            </Button>
          </div>
        ) : null}
      </Card>

      {showStats && isManager ? (
        <MakerStats
          from={state.date_from || state.date || todayISO()}
          to={state.date_to || state.date || todayISO()}
          onPick={(makerId) => update({ maker_id: [makerId], page: 1 })}
        />
      ) : null}

      <Card>
        {docSets.isLoading ? (
          <p className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Spinner /> {t("common.loading")}
          </p>
        ) : rows.length === 0 && !docSets.isError ? (
          <EmptyState title={t("common.noResults")} hint={t("common.noResultsHint")} />
        ) : null}
        {rows.length ? (
          <ResumesTable
            rows={rows}
            pagination={docSets.data?.pagination}
            loading={docSets.isLoading}
            error={docSets.error}
            role={user?.role ?? "maker"}
            selection={selection}
            onSelectionChange={setSelection}
            onOpen={setOpenRow}
            onChanged={refresh}
            showSelection
            sort={{ key: state.sort, dir: state.order === "desc" ? "desc" : "asc" }}
            onSort={(key) =>
              update({ sort: key, order: state.sort === key && state.order === "asc" ? "desc" : "asc", page: 1 })
            }
          />
        ) : null}
        <Pager pagination={docSets.data?.pagination} onChange={(page) => update({ page })} />
      </Card>

      <DocSetDrawer
        row={openRow}
        role={user?.role ?? "maker"}
        onClose={() => setOpenRow(null)}
        onCancelled={() => {
          refresh();
          setOpenRow(null);
        }}
      />
    </div>
  );
}
