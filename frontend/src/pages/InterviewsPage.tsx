import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { CalendarClock, Check, ClipboardList, Eye, ListChecks, Plus, Settings2, X } from "lucide-react";
import { api, errorMessage } from "../lib/api";
import { useAuth } from "../auth/AuthProvider";
import { t } from "../i18n";
import { Badge, Button, Card, CardHeader, EmptyState, Input, Spinner } from "../ui/primitives";
import { MultiSelect } from "../ui/MultiSelect";
import { Pager, Table, TableState } from "../ui/table";
import { Tabs } from "../ui/tabs";
import { useUrlState } from "../lib/useUrlState";
import { cn } from "../lib/utils";
import { formatDateTime, formatSeq, todayISO } from "../lib/format";
import { DateSelector } from "../components/DateSelector";
import { InterviewDrawer } from "./interviews/InterviewDrawer";
import { ScheduleDialog } from "./interviews/ScheduleDialog";
import { TemplatesDialog } from "./interviews/TemplatesDialog";
import { TaxonomyDialog } from "./interviews/TaxonomyDialog";
import { CreateInterviewDialog } from "./interviews/CreateInterviewDialog";
import type { Interview, InterviewStatus, InterviewStep, Paginated, Profile } from "../types";

interface SelectedRow {
  doc_set_id: string;
  company_name: string | null;
  job_title: string | null;
  maker_name: string | null;
  profile_id: string | null;
  selected_at: string | null;
  seq_no: number | null;
}

const DEFAULTS = {
  tab: "interviews",
  q: "",
  status: "",
  flow: "all",
  step: [] as string[],
  profile: [] as string[],
  date_from: "",
  date_to: "",
  page: 1,
};

interface InterviewCounts {
  total: number;
  todo: number;
  done: number;
}

const FLOWS = ["all", "new", "done"] as const;
type Flow = (typeof FLOWS)[number];

export function InterviewsPage() {
  const { user } = useAuth();
  const isManager = user?.role === "manager";
  const { state, update } = useUrlState(DEFAULTS);
  const [openId, setOpenId] = useState<string | null>(null);
  const [scheduleFor, setScheduleFor] = useState<string | null>(null);
  const [templatesOpen, setTemplatesOpen] = useState(false);
  const [taxonomyOpen, setTaxonomyOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);

  const reviewerTabs = ["all", "todo", "done"] as const;
  const tab = isManager
    ? state.tab === "selected"
      ? "selected"
      : "interviews"
    : reviewerTabs.includes(state.tab as (typeof reviewerTabs)[number])
      ? state.tab
      : "todo";
  const rangeActive = Boolean(state.date_from && state.date_to);
  const flow: Flow = FLOWS.includes(state.flow as Flow) ? (state.flow as Flow) : "all";

  const taxonomy = useQuery({
    queryKey: ["interview-taxonomy"],
    queryFn: () => api.get<{ steps: InterviewStep[]; statuses: InterviewStatus[] }>("/interview-taxonomy"),
    enabled: isManager,
  });

  const profiles = useQuery({
    queryKey: ["profiles", { forFilter: true }],
    queryFn: () => api.get<Paginated<Profile>>("/profiles", { page_size: 200 }),
    enabled: isManager,
  });

  const interviews = useQuery({
    queryKey: [
      "interviews",
      {
        tab,
        q: state.q,
        status: state.status,
        flow,
        step: state.step,
        profile: state.profile,
        from: state.date_from,
        to: state.date_to,
        page: state.page,
      },
    ],
    queryFn: () =>
      api.get<Paginated<Interview> & { unseen: number; counts?: InterviewCounts | null }>("/interviews", {
        tab,
        q: state.q || undefined,
        status: state.status || undefined,
        flow: flow === "all" ? undefined : flow,
        step_id: state.step.length ? state.step : undefined,
        profile_id: state.profile.length ? state.profile : undefined,
        date_from: rangeActive ? `${state.date_from}T00:00:00` : undefined,
        date_to: rangeActive ? `${state.date_to}T23:59:59` : undefined,
        page: state.page,
        page_size: 50,
      }),
  });

  const stepOptions = useMemo(
    () => (taxonomy.data?.steps ?? []).map((step) => ({
      value: step.id,
      label: step.name,
      hint: step.color ?? undefined,
    })),
    [taxonomy.data],
  );
  const profileOptions = useMemo(
    () => (profiles.data?.items ?? []).map((profile) => ({ value: profile.id, label: profile.name })),
    [profiles.data],
  );
  const stepById = useMemo(() => {
    const map = new Map<string, InterviewStep>();
    for (const step of taxonomy.data?.steps ?? []) map.set(step.id, step);
    return map;
  }, [taxonomy.data]);

  const nextUp = (interviews.data?.items ?? [])
    .filter((item) => item.status === "scheduled" && item.meeting_at && new Date(item.meeting_at).getTime() >= Date.now())
    .sort((a, b) => new Date(a.meeting_at ?? 0).getTime() - new Date(b.meeting_at ?? 0).getTime())[0];

  useEffect(() => {
    if (!isManager && interviews.data) {
      window.dispatchEvent(new CustomEvent("rf:unseen-interviews", { detail: { count: interviews.data.unseen } }));
    }
  }, [interviews.data, isManager]);

  const rows = interviews.data?.items ?? [];
  const counts = interviews.data?.counts ?? null;
  const filtersActive = flow !== "all" || state.step.length > 0 || state.profile.length > 0 || Boolean(state.status);

  const tableColumns = isManager ? 9 : 8;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{t("interviews.title")}</h1>
          {!isManager && interviews.data?.unseen ? (
            <p className="text-sm text-muted-foreground">{t("interviews.newBadge", { count: interviews.data.unseen })}</p>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <DateSelector
            date={state.date_from || todayISO()}
            onDate={(value) => update({ date_from: value, date_to: value, page: 1 })}
            range={{ from: state.date_from, to: state.date_to }}
            onRange={(value) => update({ date_from: value.from, date_to: value.to, page: 1 })}
            withRange
          />
          <Input
            className="h-8 w-56 text-sm"
            placeholder={t("interviews.searchPlaceholder")}
            value={state.q}
            onChange={(event) => update({ q: event.target.value, page: 1 })}
          />
          {isManager ? (
            <>
              <Button size="sm" variant="outline" onClick={() => setTaxonomyOpen(true)}>
                <Settings2 className="h-3.5 w-3.5" />
                {t("interviews.stepsAndStatuses")}
              </Button>
              <Button size="sm" variant="outline" onClick={() => setTemplatesOpen(true)}>
                <ClipboardList className="h-3.5 w-3.5" />
                {t("interviews.templates")}
              </Button>
              <Button size="sm" onClick={() => setCreateOpen(true)}>
                <Plus className="h-3.5 w-3.5" />
                {t("interviews.createNew")}
              </Button>
            </>
          ) : null}
        </div>
      </div>

      {!isManager ? (
        <div className="grid gap-3 tablet:grid-cols-2">
          <Card>
            <CardHeader title={t("interviews.nextUp")} />
            {nextUp ? (
              <div className="text-sm">
                <p className="font-medium">{nextUp.doc_set?.company_name ?? nextUp.company_name ?? "—"}</p>
                <p className="text-muted-foreground">
                  {nextUp.doc_set?.job_title ?? nextUp.job_title ?? "—"} · {formatDateTime(nextUp.meeting_at)}
                </p>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">{t("interviews.upcomingEmpty")}</p>
            )}
          </Card>
          <Card>
            <CardHeader title={t("interviews.overview")} />
            <dl className="grid grid-cols-3 gap-2 text-sm">
              <div>
                <dt className="rf-label">{t("interviews.overviewTotal")}</dt>
                <dd className="text-2xl font-semibold">{counts?.total ?? rows.length}</dd>
              </div>
              <div>
                <dt className="rf-label">{t("interviews.overviewTodo")}</dt>
                <dd className="text-2xl font-semibold">{counts?.todo ?? "—"}</dd>
              </div>
              <div>
                <dt className="rf-label">{t("interviews.overviewDone")}</dt>
                <dd className="text-2xl font-semibold">{counts?.done ?? "—"}</dd>
              </div>
            </dl>
          </Card>
        </div>
      ) : null}

      {isManager ? (
        <Tabs
          items={[
            { id: "selected", label: t("interviews.tabSelected") },
            { id: "interviews", label: t("interviews.tabInterviews") },
          ]}
          value={tab}
          onChange={(id) => update({ tab: id, page: 1 })}
        />
      ) : (
        <Tabs
          items={[
            { id: "all", label: t("interviews.tabAll") },
            { id: "todo", label: t("interviews.tabTodo"), badge: interviews.data?.unseen },
            { id: "done", label: t("interviews.tabDone") },
          ]}
          value={tab}
          onChange={(id) => update({ tab: id, page: 1 })}
        />
      )}

      {isManager && tab === "selected" ? (
        <SelectedDocSets onSchedule={setScheduleFor} />
      ) : (
        <Card>
          {isManager ? (
            <div className="mb-2 flex flex-wrap items-center gap-2 border-b border-border pb-2">
              <div className="flex items-center gap-1 rounded-md border border-border p-0.5">
                {FLOWS.map((value) => (
                  <button
                    key={value}
                    type="button"
                    className={cn(
                      "rounded px-2 py-1 text-xs",
                      flow === value ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-accent",
                    )}
                    onClick={() => update({ flow: value, page: 1 })}
                  >
                    {t(`interviews.flow${value.charAt(0).toUpperCase()}${value.slice(1)}`)}
                  </button>
                ))}
              </div>
              <MultiSelect
                options={stepOptions}
                values={state.step}
                onChange={(values) => update({ step: values, page: 1 })}
                placeholder={t("interviews.filterStep")}
                className="w-44"
              />
              <MultiSelect
                options={profileOptions}
                values={state.profile}
                onChange={(values) => update({ profile: values, page: 1 })}
                placeholder={t("interviews.filterProfile")}
                className="w-44"
              />
              {filtersActive ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => update({ flow: "all", step: [], profile: [], status: "", page: 1 })}
                >
                  {t("common.clearFilters")}
                </Button>
              ) : null}
              <span className="ml-auto text-xs text-muted-foreground">{t("interviews.filterFlowHint")}</span>
            </div>
          ) : null}
          {interviews.isLoading ? (
            <p className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
              <Spinner /> {t("common.loading")}
            </p>
          ) : rows.length === 0 && !interviews.isError ? (
            <EmptyState title={tab === "past" ? t("interviews.pastEmpty") : t("interviews.upcomingEmpty")} />
          ) : null}
          {rows.length ? (
            <Table>
              <thead>
                <tr>
                  <th>{t("interviews.stepColumn")}</th>
                  {isManager ? <th>{t("resumes.columns.company")}</th> : null}
                  <th>{t("resumes.columns.title")}</th>
                  <th>{t("interviews.techStack")}</th>
                  {isManager ? <th>{t("interviews.reviewer")}</th> : null}
                  <th>{t("interviews.meetingAt")}</th>
                  <th>{t("common.status")}</th>
                  <th>{t("interviews.feedback")}</th>
                  {!isManager ? <th>{t("interviews.available")}</th> : null}
                  <th className="text-right">{t("common.view")}</th>
                </tr>
              </thead>
              <tbody>
                {interviews.error ? (
                  <TableState loading={false} error={errorMessage(interviews.error)} empty={t("common.noResults")} colSpan={tableColumns} />
                ) : null}
                {rows.map((row) => {
                  const isDone = row.status === "completed" || row.status === "cancelled";
                  const company = row.doc_set?.company_name ?? row.company_name ?? "—";
                  const title = row.doc_set?.job_title ?? row.job_title ?? "—";
                  return (
                    <tr
                      key={row.id}
                      className={cn("cursor-pointer hover:bg-accent/40", isDone && "bg-muted/30 text-muted-foreground")}
                      onClick={() => setOpenId(row.id)}
                    >
                      <td>
                        <StepTags steps={row.steps} stepById={stepById} />
                      </td>
                      {isManager ? <td className="font-medium">{company}</td> : null}
                      <td>{title}</td>
                      <td className="max-w-[12rem] truncate text-xs" title={row.tech_stack ?? undefined}>
                        {row.tech_stack || "—"}
                      </td>
                      {isManager ? <td>{row.reviewer_name ?? "—"}</td> : null}
                      <td className="whitespace-nowrap text-xs" title={row.meeting_tz ?? undefined}>
                        {formatDateTime(row.meeting_at)}
                      </td>
                      <td>
                        <InterviewStatusBadge row={row} />
                      </td>
                      <td>{row.feedback ? <Badge tone="success">{t(`interviews.outcomes.${row.feedback.outcome}`)}</Badge> : "—"}</td>
                      {!isManager ? (
                        <td>
                          {row.reviewer_id && row.status === "scheduled" ? (
                            <Badge tone="success">{t("common.yes")}</Badge>
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </td>
                      ) : null}
                      <td className="text-right">
                        <Eye className="inline h-3.5 w-3.5 text-muted-foreground" />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </Table>
          ) : null}
          <Pager pagination={interviews.data?.pagination} onChange={(page) => update({ page })} />
        </Card>
      )}

      <ScheduleDialog
        open={Boolean(scheduleFor)}
        docSetId={scheduleFor}
        onClose={() => setScheduleFor(null)}
        onCreated={() => {
          update({ tab: "interviews", page: 1 });
          void interviews.refetch();
        }}
      />
      <TemplatesDialog open={templatesOpen} onClose={() => setTemplatesOpen(false)} />
      <TaxonomyDialog open={taxonomyOpen} onClose={() => setTaxonomyOpen(false)} />
      <CreateInterviewDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={() => {
          update({ tab: "interviews", flow: "all", page: 1 });
          void interviews.refetch();
        }}
      />
      <InterviewDrawer interviewId={openId} role={user?.role ?? "reviewer"} onClose={() => setOpenId(null)} />
    </div>
  );
}

/** INT-14: the interview's step trail — the current step is highlighted, earlier ones are dimmed. */
function StepTags({ steps, stepById }: { steps: Interview["steps"]; stepById: Map<string, InterviewStep> }) {
  if (!steps.length) return <span className="text-muted-foreground">—</span>;
  const currentIndex = steps.length - 1;
  return (
    <div className="flex flex-wrap items-center gap-1">
      {steps.map((record, index) => {
        const active = index === currentIndex;
        const color = record.step_color ?? (record.step_id ? stepById.get(record.step_id)?.color : null) ?? "#64748b";
        return (
          <span
            key={record.id}
            className={cn(
              "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs whitespace-nowrap",
              active ? "font-medium" : "opacity-60",
            )}
            style={
              active
                ? { borderColor: color, color, backgroundColor: `${color}1a` }
                : { borderColor: `${color}66`, color }
            }
            title={record.note ?? undefined}
          >
            {record.done ? <Check className="h-3 w-3" /> : null}
            {record.rejected ? <X className="h-3 w-3" /> : null}
            {record.step_name ?? "—"}
          </span>
        );
      })}
    </div>
  );
}

function InterviewStatusBadge({ row }: { row: Interview }) {
  const label = row.status_label;
  if (label) {
    const color = label.color ?? undefined;
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium"
        style={color ? { borderColor: color, color, backgroundColor: `${color}1a` } : undefined}
      >
        {label.name}
      </span>
    );
  }
  const tone =
    row.status === "completed" ? "success" : row.status === "cancelled" ? "neutral" : row.status === "no_show" ? "warning" : "info";
  return <Badge tone={tone}>{t(`interviews.status.${row.status}`)}</Badge>;
}

function SelectedDocSets({ onSchedule }: { onSchedule: (docSetId: string) => void }) {
  const selected = useQuery({
    queryKey: ["interviews", { tab: "selected" }],
    queryFn: () => api.get<Paginated<SelectedRow>>("/interviews", { tab: "selected", page_size: 200 }),
  });
  const rows = selected.data?.items ?? [];

  if (selected.isLoading) {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <Spinner /> {t("common.loading")}
      </p>
    );
  }
  if (!rows.length) return <EmptyState title={t("interviews.selectedEmpty")} />;

  return (
    <Card>
      <Table>
        <thead>
          <tr>
            <th>{t("resumes.columns.company")}</th>
            <th>{t("resumes.columns.title")}</th>
            <th>{t("resumes.columns.maker")}</th>
            <th>{t("interviews.selectedDate")}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.doc_set_id}>
              <td className="font-medium">
                {formatSeq(row.seq_no)} · {row.company_name ?? "—"}
              </td>
              <td>{row.job_title ?? "—"}</td>
              <td>{row.maker_name ?? "—"}</td>
              <td className="text-xs">{formatDateTime(row.selected_at)}</td>
              <td className="text-right">
                <Button size="sm" onClick={() => onSchedule(row.doc_set_id)}>
                  <CalendarClock className="h-3.5 w-3.5" />
                  {t("interviews.schedule")}
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </Table>
      <p className="mt-2 text-xs text-muted-foreground">
        <ListChecks className="mr-1 inline h-3.5 w-3.5" />
        {t("interviews.selectedEmpty")}
      </p>
    </Card>
  );
}
