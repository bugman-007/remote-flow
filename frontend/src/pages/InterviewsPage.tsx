import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { CalendarClock, ClipboardList, ListChecks } from "lucide-react";
import { api, errorMessage } from "../lib/api";
import { useAuth } from "../auth/AuthProvider";
import { t } from "../i18n";
import { Badge, Button, Card, CardHeader, EmptyState, Input, Select, Spinner } from "../ui/primitives";
import { Pager, Table, TableState } from "../ui/table";
import { Tabs } from "../ui/tabs";
import { useUrlState } from "../lib/useUrlState";
import { formatDateTime, formatSeq } from "../lib/format";
import { InterviewDrawer } from "./interviews/InterviewDrawer";
import { ScheduleDialog } from "./interviews/ScheduleDialog";
import { TemplatesDialog } from "./interviews/TemplatesDialog";
import type { Interview, Paginated } from "../types";

interface SelectedRow {
  doc_set_id: string;
  company_name: string | null;
  job_title: string | null;
  maker_name: string | null;
  profile_id: string | null;
  selected_at: string | null;
  seq_no: number | null;
}

const DEFAULTS = { tab: "interviews", q: "", status: "", page: 1 };

export function InterviewsPage() {
  const { user } = useAuth();
  const isManager = user?.role === "manager";
  const { state, update } = useUrlState(DEFAULTS);
  const [openId, setOpenId] = useState<string | null>(null);
  const [scheduleFor, setScheduleFor] = useState<string | null>(null);
  const [templatesOpen, setTemplatesOpen] = useState(false);

  const tab = isManager ? state.tab || "interviews" : state.tab === "past" ? "past" : "upcoming";

  const interviews = useQuery({
    queryKey: ["interviews", { tab, q: state.q, status: state.status, page: state.page }],
    queryFn: () =>
      api.get<Paginated<Interview> & { unseen: number }>("/interviews", {
        tab,
        q: state.q || undefined,
        status: state.status || undefined,
        page: state.page,
        page_size: 50,
      }),
  });

  const nextUp = (interviews.data?.items ?? [])
    .filter((item) => item.status === "scheduled" && item.meeting_at && new Date(item.meeting_at).getTime() >= Date.now())
    .sort((a, b) => new Date(a.meeting_at ?? 0).getTime() - new Date(b.meeting_at ?? 0).getTime())[0];

  useEffect(() => {
    if (!isManager && interviews.data) {
      window.dispatchEvent(new CustomEvent("rf:unseen-interviews", { detail: { count: interviews.data.unseen } }));
    }
  }, [interviews.data, isManager]);

  const rows = interviews.data?.items ?? [];

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
          <Input
            className="h-8 w-56 text-sm"
            placeholder={t("interviews.searchPlaceholder")}
            value={state.q}
            onChange={(event) => update({ q: event.target.value, page: 1 })}
          />
          {isManager ? (
            <>
              <Select
                className="h-8 w-36 text-xs"
                value={state.status}
                onChange={(event) => update({ status: event.target.value, page: 1 })}
              >
                <option value="">{t("common.all")}</option>
                {["scheduled", "completed", "cancelled", "no_show"].map((status) => (
                  <option key={status} value={status}>
                    {t(`interviews.status.${status}`)}
                  </option>
                ))}
              </Select>
              <Button size="sm" variant="outline" onClick={() => setTemplatesOpen(true)}>
                <ClipboardList className="h-3.5 w-3.5" />
                {t("interviews.templates")}
              </Button>
            </>
          ) : null}
        </div>
      </div>

      {!isManager ? (
        <div className="grid gap-3 tablet:grid-cols-3">
          <Card>
            <CardHeader title={t("interviews.nextUp")} />
            {nextUp ? (
              <div className="text-sm">
                <p className="font-medium">{nextUp.doc_set?.company_name ?? "—"}</p>
                <p className="text-muted-foreground">{formatDateTime(nextUp.meeting_at)}</p>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">{t("interviews.upcomingEmpty")}</p>
            )}
          </Card>
          <Card>
            <CardHeader title={t("interviews.thisWeek")} />
            <p className="text-2xl font-semibold">{rows.filter((row) => row.status === "scheduled").length}</p>
          </Card>
          <Card>
            <CardHeader title={t("interviews.pendingFeedback")} />
            <p className="text-2xl font-semibold">
              {rows.filter((row) => !row.feedback && row.meeting_at && new Date(row.meeting_at).getTime() <= Date.now()).length}
            </p>
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
            { id: "upcoming", label: t("interviews.tabUpcoming"), badge: interviews.data?.unseen },
            { id: "past", label: t("interviews.tabPast") },
          ]}
          value={tab}
          onChange={(id) => update({ tab: id, page: 1 })}
        />
      )}

      {isManager && tab === "selected" ? (
        <SelectedDocSets onSchedule={setScheduleFor} />
      ) : (
        <Card>
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
                  <th>{t("resumes.columns.company")}</th>
                  <th>{t("resumes.columns.title")}</th>
                  {isManager ? <th>{t("interviews.reviewer")}</th> : null}
                  <th>{t("interviews.meetingAt")}</th>
                  <th>{t("common.status")}</th>
                  <th>{t("interviews.pinnedGeneration", { n: "" })}</th>
                  <th>{t("interviews.feedback")}</th>
                </tr>
              </thead>
              <tbody>
                <TableState loading={false} error={interviews.error ? errorMessage(interviews.error) : undefined} empty={t("common.noResults")} colSpan={isManager ? 7 : 6} />
                {rows.map((row) => (
                  <tr key={row.id} className="cursor-pointer hover:bg-accent/40" onClick={() => setOpenId(row.id)}>
                    <td className="font-medium">{row.doc_set?.company_name ?? "—"}</td>
                    <td>{row.doc_set?.job_title ?? "—"}</td>
                    {isManager ? <td>{row.reviewer_name ?? "—"}</td> : null}
                    <td className="whitespace-nowrap text-xs" title={row.meeting_tz ?? undefined}>
                      {formatDateTime(row.meeting_at)}
                    </td>
                    <td>
                      <Badge tone={row.status === "completed" ? "success" : row.status === "cancelled" ? "neutral" : row.status === "no_show" ? "warning" : "info"}>
                        {t(`interviews.status.${row.status}`)}
                      </Badge>
                    </td>
                    <td>
                      {row.pinned_generation_no ? (
                        <span className="flex items-center gap-1">
                          gen {row.pinned_generation_no}
                          {row.newer_generation_available ? <Badge tone="warning">{t("interviews.newerAvailable")}</Badge> : null}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>{row.feedback ? <Badge tone="success">{t(`interviews.outcomes.${row.feedback.outcome}`)}</Badge> : "—"}</td>
                  </tr>
                ))}
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
      <InterviewDrawer interviewId={openId} role={user?.role ?? "reviewer"} onClose={() => setOpenId(null)} />
    </div>
  );
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
