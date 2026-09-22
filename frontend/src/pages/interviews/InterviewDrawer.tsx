import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarPlus, Copy, Eye, Repeat } from "lucide-react";
import { api, downloadBlob, errorMessage } from "../../lib/api";
import { Drawer } from "../../ui/dialog";
import { Badge, Button, Card, CardHeader, ErrorNote, Field, Input, Select, Spinner, Textarea } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import { FileChips } from "../../components/FileChips";
import { formatDateTime } from "../../lib/format";
import { t } from "../../i18n";
import type { Feedback, Interview, User } from "../../types";
import type { Paginated } from "../../types";

export function InterviewDrawer({
  interviewId,
  role,
  onClose,
}: {
  interviewId: string | null;
  role: string;
  onClose: () => void;
}) {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const isManager = role === "manager";

  const interview = useQuery({
    queryKey: ["interview", interviewId],
    queryFn: () => api.get<Interview>(`/interviews/${interviewId}`),
    enabled: Boolean(interviewId),
  });

  const reviewers = useQuery({
    queryKey: ["users", { role: "reviewer" }],
    queryFn: () => api.get<Paginated<User>>("/users", { role: "reviewer", page_size: 200 }),
    enabled: Boolean(interviewId) && isManager,
  });

  const data = interview.data;
  const [feedback, setFeedback] = useState<Partial<Feedback>>({});
  const [values, setValues] = useState<Record<string, unknown>>({});

  useEffect(() => {
    if (data?.feedback) setFeedback(data.feedback);
  }, [data?.feedback]);

  useEffect(() => {
    if (data?.values) setValues(data.values);
  }, [data?.values]);

  const refresh = async () => {
    await interview.refetch();
    void queryClient.invalidateQueries({ queryKey: ["interviews"] });
  };

  const patch = async (body: Record<string, unknown>, message?: string) => {
    try {
      await api.patch(`/interviews/${interviewId}`, body);
      if (message) push({ tone: "success", title: message });
      await refresh();
    } catch (error) {
      push({ tone: "error", title: errorMessage(error) });
    }
  };

  const markSeen = async () => {
    try {
      await api.post(`/interviews/${interviewId}/seen`);
      await refresh();
    } catch (error) {
      push({ tone: "error", title: errorMessage(error) });
    }
  };

  const submitFeedback = async () => {
    try {
      await api.post(`/interviews/${interviewId}/feedback`, {
        outcome: feedback.outcome ?? "hold",
        rating: feedback.rating ?? null,
        strengths: feedback.strengths ?? null,
        concerns: feedback.concerns ?? null,
        notes: feedback.notes ?? null,
      });
      push({ tone: "success", title: t("interviews.feedbackSaved") });
      await refresh();
    } catch (error) {
      push({ tone: "error", title: errorMessage(error) });
    }
  };

  const fields = (data?.template_snapshot?.fields ?? []).filter((field) => !field.builtin || field.key !== "reviewer");
  const meetingPassed = data?.meeting_at ? new Date(data.meeting_at).getTime() <= Date.now() : false;
  const canGiveFeedback = !isManager && (meetingPassed || data?.status === "completed" || data?.status === "no_show");

  return (
    <Drawer
      open={Boolean(interviewId)}
      onClose={onClose}
      title={
        data ? (
          <span className="flex flex-wrap items-center gap-2">
            {data.doc_set?.company_name ?? t("common.unknown")} · {data.doc_set?.job_title ?? ""}
            <Badge tone={data.status === "completed" ? "success" : data.status === "cancelled" ? "neutral" : "info"}>
              {t(`interviews.status.${data.status}`)}
            </Badge>
            {data.pinned_generation_no ? <Badge tone="outline">{t("interviews.pinnedGeneration", { n: data.pinned_generation_no })}</Badge> : null}
            {data.newer_generation_available ? <Badge tone="warning">{t("interviews.newerAvailable")}</Badge> : null}
          </span>
        ) : (
          t("interviews.detail")
        )
      }
    >
      {interview.isLoading ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> {t("common.loading")}
        </p>
      ) : interview.isError ? (
        <ErrorNote>{errorMessage(interview.error)}</ErrorNote>
      ) : data ? (
        <div className="space-y-4">
          {isManager && data.newer_generation_available && data.doc_set?.current_generation_id ? (
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={async () => {
                  try {
                    await api.post(`/interviews/${data.id}/use-generation`, {
                      generation_id: data.doc_set?.current_generation_id,
                    });
                    push({ tone: "success", title: t("toast.updated") });
                    await refresh();
                  } catch (error) {
                    push({ tone: "error", title: errorMessage(error) });
                  }
                }}
              >
                <Repeat className="h-3.5 w-3.5" />
                {t("interviews.updateGeneration", { n: (data.pinned_generation_no ?? 0) + 1 })}
              </Button>
            </div>
          ) : null}

          <Card>
            <CardHeader title={t("interviews.detail")} description={formatDateTime(data.meeting_at)} />
            <dl className="grid gap-2 text-sm tablet:grid-cols-2">
              <div>
                <dt className="rf-label">{t("interviews.reviewer")}</dt>
                <dd>{data.reviewer_name ?? "—"}</dd>
              </div>
              <div>
                <dt className="rf-label">{t("interviews.meetingAt")}</dt>
                <dd>
                  {data.meeting_at ? new Date(data.meeting_at).toLocaleString() : "—"}
                  <span className="ml-1 text-xs text-muted-foreground" title={t("interviews.meetingTz", { tz: data.meeting_tz ?? "UTC" })}>
                    ({data.meeting_tz ?? "UTC"})
                  </span>
                </dd>
              </div>
            </dl>
          </Card>

          <Card>
            <CardHeader title={t("resumes.columns.files")} />
            <FileChips files={data.files} />
            {data.files.length ? (
              <Button
                className="mt-2"
                size="sm"
                variant="outline"
                onClick={async () => {
                  try {
                    const blob = await api.requestBlob(`/doc-sets/${data.doc_set_id}/zip`, {
                      query: { generation: data.generation_id },
                    });
                    await downloadBlob(blob, `documents-${data.doc_set?.company_name ?? data.id}.zip`);
                  } catch (error) {
                    push({ tone: "error", title: errorMessage(error) });
                  }
                }}
              >
                {t("resumes.downloadSet")}
              </Button>
            ) : null}
          </Card>

          {isManager && data.profile ? (
            <Card>
              <CardHeader title={t("profiles.title")} />
              <dl className="grid gap-2 text-sm tablet:grid-cols-2">
                {Object.entries(data.profile)
                  .filter(([, value]) => value !== null && value !== undefined)
                  .map(([key, value]) => (
                    <div key={key}>
                      <dt className="rf-label">{key.replace(/_/g, " ")}</dt>
                      <dd className="break-words">{Array.isArray(value) ? value.join(", ") : String(value)}</dd>
                    </div>
                  ))}
              </dl>
            </Card>
          ) : null}

          {!isManager && data.profile ? (
            <Card>
              <CardHeader title={t("interviews.sharedProfile")} />
              <dl className="grid gap-2 text-sm tablet:grid-cols-2">
                {Object.entries(data.profile).map(([key, value]) => (
                  <div key={key}>
                    <dt className="rf-label">{key.replace(/_/g, " ")}</dt>
                    <dd className="break-words">{String(value)}</dd>
                  </div>
                ))}
              </dl>
            </Card>
          ) : null}

          <Card>
            <CardHeader title={t("interviews.detail")} />
            <div className="grid gap-3 tablet:grid-cols-2">
              {fields
                .filter((field) => isManager || field.visible_to_reviewer)
                .map((field) => (
                  <div key={field.key}>
                    <p className="rf-label">{field.label}</p>
                    {isManager ? (
                      field.type === "textarea" ? (
                        <Textarea
                          value={String(values[field.key] ?? "")}
                          onChange={(event) => setValues({ ...values, [field.key]: event.target.value })}
                        />
                      ) : (
                        <Input
                          value={String(values[field.key] ?? "")}
                          onChange={(event) => setValues({ ...values, [field.key]: event.target.value })}
                        />
                      )
                    ) : field.type === "url" && values[field.key] ? (
                      <a className="text-sm underline" href={String(values[field.key])} target="_blank" rel="noreferrer">
                        {String(values[field.key])}
                      </a>
                    ) : (
                      <p className="text-sm">{String(values[field.key] ?? "—")}</p>
                    )}
                  </div>
                ))}
            </div>
            {isManager ? (
              <Button className="mt-3" size="sm" onClick={() => void patch({ values }, t("toast.saved"))}>
                {t("common.save")}
              </Button>
            ) : null}
          </Card>

          {isManager ? (
            <Card>
              <CardHeader title={t("common.actions")} />
              <div className="flex flex-wrap items-center gap-2">
                <Select
                  className="h-8 w-48 text-xs"
                  value={data.reviewer_id ?? ""}
                  onChange={(event) => void patch({ reviewer_id: event.target.value }, t("toast.updated"))}
                >
                  <option value="">—</option>
                  {(reviewers.data?.items ?? []).map((reviewer) => (
                    <option key={reviewer.id} value={reviewer.id}>
                      {reviewer.name}
                    </option>
                  ))}
                </Select>
                <Button size="sm" variant="outline" onClick={() => void patch({ status: "completed" }, t("toast.updated"))}>
                  {t("interviews.status.completed")}
                </Button>
                <Button size="sm" variant="outline" onClick={() => void patch({ status: "no_show" }, t("toast.updated"))}>
                  {t("interviews.status.no_show")}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={async () => {
                    const reviewerId = window.prompt(t("interviews.duplicateTo"), data.reviewer_id ?? "");
                    if (!reviewerId) return;
                    try {
                      await api.post(`/interviews/${data.id}/duplicate`, { reviewer_id: reviewerId });
                      push({ tone: "success", title: t("toast.saved") });
                      void queryClient.invalidateQueries({ queryKey: ["interviews"] });
                    } catch (error) {
                      push({ tone: "error", title: errorMessage(error) });
                    }
                  }}
                >
                  <Copy className="h-3.5 w-3.5" />
                  {t("interviews.duplicate")}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={async () => {
                    try {
                      const blob = await api.blob(`/interviews/${data.id}/ics`);
                      await downloadBlob(blob, `interview-${data.doc_set?.company_name ?? data.id}.ics`);
                    } catch (error) {
                      push({ tone: "error", title: errorMessage(error) });
                    }
                  }}
                >
                  <CalendarPlus className="h-3.5 w-3.5" />
                  {t("interviews.downloadIcs")}
                </Button>
                <Button
                  size="sm"
                  variant="destructive"
                  onClick={async () => {
                    try {
                      await api.post(`/interviews/${data.id}/cancel`);
                      await refresh();
                    } catch (error) {
                      push({ tone: "error", title: errorMessage(error) });
                    }
                  }}
                >
                  {t("interviews.cancel")}
                </Button>
              </div>
            </Card>
          ) : null}

          {!isManager && !data.seen_by_reviewer_at ? (
            <Button size="sm" variant="outline" onClick={() => void markSeen()}>
              <Eye className="h-3.5 w-3.5" />
              {t("interviews.seen")}
            </Button>
          ) : null}

          {data.feedback ? (
            <Card>
              <CardHeader
                title={
                  <span className="flex items-center gap-2">
                    {t("interviews.feedback")}
                    {data.feedback.edited ? <Badge tone="outline">{t("interviews.feedbackEditedMarker")}</Badge> : null}
                  </span>
                }
                description={formatDateTime(data.feedback.created_at)}
              />
              <dl className="grid gap-2 text-sm">
                <div>
                  <dt className="rf-label">{t("interviews.feedbackOutcome")}</dt>
                  <dd>{t(`interviews.outcomes.${data.feedback.outcome}`)}</dd>
                </div>
                <div>
                  <dt className="rf-label">{t("interviews.feedbackRating")}</dt>
                  <dd>{data.feedback.rating ?? "—"} / 5</dd>
                </div>
                <div>
                  <dt className="rf-label">{t("interviews.feedbackStrengths")}</dt>
                  <dd>{data.feedback.strengths ?? "—"}</dd>
                </div>
                <div>
                  <dt className="rf-label">{t("interviews.feedbackConcerns")}</dt>
                  <dd>{data.feedback.concerns ?? "—"}</dd>
                </div>
                <div>
                  <dt className="rf-label">{t("interviews.feedbackNotes")}</dt>
                  <dd>{data.feedback.notes ?? "—"}</dd>
                </div>
              </dl>
            </Card>
          ) : null}

          {canGiveFeedback ? (
            <Card>
              <CardHeader title={data.feedback ? t("interviews.feedbackEdit") : t("interviews.feedback")} description={t("interviews.meetingTz", { tz: data.meeting_tz ?? "UTC" })} />
              <div className="grid gap-3 tablet:grid-cols-2">
                <Field label={t("interviews.feedbackOutcome")}>
                  <Select
                    value={feedback.outcome ?? "hold"}
                    onChange={(event) => setFeedback({ ...feedback, outcome: event.target.value as Feedback["outcome"] })}
                  >
                    {["pass", "fail", "hold", "no_show"].map((outcome) => (
                      <option key={outcome} value={outcome}>
                        {t(`interviews.outcomes.${outcome}`)}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label={t("interviews.feedbackRating")}>
                  <Select
                    value={String(feedback.rating ?? "")}
                    onChange={(event) => setFeedback({ ...feedback, rating: Number(event.target.value) })}
                  >
                    <option value="">—</option>
                    {[1, 2, 3, 4, 5].map((rating) => (
                      <option key={rating} value={rating}>
                        {rating}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label={t("interviews.feedbackStrengths")}>
                  <Textarea value={feedback.strengths ?? ""} onChange={(event) => setFeedback({ ...feedback, strengths: event.target.value })} />
                </Field>
                <Field label={t("interviews.feedbackConcerns")}>
                  <Textarea value={feedback.concerns ?? ""} onChange={(event) => setFeedback({ ...feedback, concerns: event.target.value })} />
                </Field>
              </div>
              <Field label={t("interviews.feedbackNotes")}>
                <Textarea value={feedback.notes ?? ""} onChange={(event) => setFeedback({ ...feedback, notes: event.target.value })} />
              </Field>
              <Button size="sm" onClick={() => void submitFeedback()}>
                {t("interviews.feedbackSubmit")}
              </Button>
            </Card>
          ) : null}

          {isManager && data.events?.length ? (
            <Card>
              <CardHeader title={t("interviews.events")} />
              <ul className="space-y-1 text-xs">
                {data.events.map((event, index) => (
                  <li key={index} className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-muted-foreground">{formatDateTime(event.at)}</span>
                    <Badge tone="outline">{event.type}</Badge>
                    <span>{JSON.stringify(event.details)}</span>
                  </li>
                ))}
              </ul>
            </Card>
          ) : null}
        </div>
      ) : null}
    </Drawer>
  );
}
