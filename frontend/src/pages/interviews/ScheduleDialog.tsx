import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, errorMessage } from "../../lib/api";
import { Dialog } from "../../ui/dialog";
import { Button, Checkbox, ErrorNote, Field, Input, Select, Textarea } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import { t } from "../../i18n";
import { todayISO } from "../../lib/format";
import type { InterviewStep, InterviewTemplate, InterviewTemplateField, Paginated, User } from "../../types";

/** INT-3: fields the dialog renders itself, so templates must not duplicate them. */
const MANAGED_FIELDS = new Set(["reviewer", "meeting_time", "location"]);

interface Props {
  open: boolean;
  docSetId: string | null;
  onClose: () => void;
  onCreated: () => void;
}

/** INT-3: schedule with the default template; the pinned generation is decided server-side. */
export function ScheduleDialog({ open, docSetId, onClose, onCreated }: Props) {
  const { push } = useToast();
  const [templateId, setTemplateId] = useState("");
  const [reviewerId, setReviewerId] = useState("");
  const [stepId, setStepId] = useState("");
  const [meetingDate, setMeetingDate] = useState(todayISO());
  const [startTime, setStartTime] = useState("");
  const [endTime, setEndTime] = useState("");
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const templates = useQuery({
    queryKey: ["interview-templates"],
    queryFn: () => api.get<{ items: InterviewTemplate[]; builtin_fields: InterviewTemplateField[]; field_types: string[] }>("/interview-templates"),
    enabled: open,
  });

  const reviewers = useQuery({
    queryKey: ["users", { role: "reviewer" }],
    queryFn: () => api.get<Paginated<User>>("/users", { role: "reviewer", page_size: 200 }),
    enabled: open,
  });

  const taxonomy = useQuery({
    queryKey: ["interview-taxonomy"],
    queryFn: () => api.get<{ steps: InterviewStep[] }>("/interview-taxonomy"),
    enabled: open,
  });

  const activeTemplate = useMemo(() => {
    const list = templates.data?.items ?? [];
    return list.find((item) => item.id === templateId) ?? list.find((item) => item.is_default) ?? list[0] ?? null;
  }, [templates.data, templateId]);

  useEffect(() => {
    setValues({});
  }, [activeTemplate?.id]);

  const submit = async () => {
    if (!docSetId) return;
    if (!meetingDate || !startTime) {
      setError(t("interviews.meetingRequired"));
      return;
    }
    if (endTime && endTime <= startTime) {
      setError(t("interviews.meetingOrder"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.post("/interviews", {
        doc_set_id: docSetId,
        reviewer_id: reviewerId || null,
        step_id: stepId || null,
        template_id: activeTemplate?.id ?? null,
        values: {
          ...values,
          meeting_date: meetingDate,
          meeting_start_time: startTime,
          meeting_end_time: endTime || null,
        },
        meeting_at: `${meetingDate}T${startTime.length === 5 ? `${startTime}:00` : startTime}`,
        meeting_tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
      });
      push({ tone: "success", title: t("interviews.saved") });
      onCreated();
      onClose();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={t("interviews.schedule")}
      description={activeTemplate ? activeTemplate.name : undefined}
      width="max-w-2xl"
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            {t("common.cancel")}
          </Button>
          <Button onClick={() => void submit()} loading={busy}>
            {t("common.save")}
          </Button>
        </>
      }
    >
      <div className="grid gap-3 tablet:grid-cols-2">
        <Field label={t("interviews.templates")}>
          <Select value={activeTemplate?.id ?? ""} onChange={(event) => setTemplateId(event.target.value)}>
            {(templates.data?.items ?? []).map((template) => (
              <option key={template.id} value={template.id}>
                {template.name}
                {template.is_default ? " ★" : ""}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={t("interviews.reviewer")}>
          <Select value={reviewerId} onChange={(event) => setReviewerId(event.target.value)}>
            <option value="">—</option>
            {(reviewers.data?.items ?? []).map((reviewer) => (
              <option key={reviewer.id} value={reviewer.id}>
                {reviewer.name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={t("interviews.step")}>
          <Select value={stepId} onChange={(event) => setStepId(event.target.value)}>
            <option value="">—</option>
            {(taxonomy.data?.steps ?? []).map((step) => (
              <option key={step.id} value={step.id}>
                {step.name}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      <div className="grid gap-3 tablet:grid-cols-3">
        <Field label={t("interviews.meetingDate")}>
          <Input type="date" value={meetingDate} onChange={(event) => setMeetingDate(event.target.value)} />
        </Field>
        <Field label={t("interviews.startTime")}>
          <Input type="time" value={startTime} onChange={(event) => setStartTime(event.target.value)} />
        </Field>
        <Field label={t("interviews.endTime")} hint={t("interviews.endTimeHint")}>
          <Input type="time" value={endTime} onChange={(event) => setEndTime(event.target.value)} />
        </Field>
      </div>

      <div className="grid gap-3 tablet:grid-cols-2">
        {(activeTemplate?.fields ?? [])
          .filter((field) => !field.builtin || !MANAGED_FIELDS.has(field.key))
          .map((field) => (
            <FieldMarkup
              key={field.key}
              field={field}
              value={values[field.key]}
              onChange={(value) => setValues((current) => ({ ...current, [field.key]: value }))}
            />
          ))}
      </div>
      <MeetingLinkHint value={values.meeting_link} />

      {error ? (
        <div className="mt-2">
          <ErrorNote>{error}</ErrorNote>
        </div>
      ) : null}
    </Dialog>
  );
}

/** INT-3: show whether a meeting link is Google Meet or Zoom (so Location is not needed). */
export function MeetingLinkHint({ value }: { value: unknown }) {
  const link = String(value ?? "");
  if (!link) return null;
  const kind = /meet\.google\.com/i.test(link) ? "Google Meet" : /zoom\.us|zoomgov\.com/i.test(link) ? "Zoom" : null;
  if (!kind) return null;
  return <p className="mt-1 text-xs text-muted-foreground">{t("interviews.linkDetected", { kind })}</p>;
}

export function FieldMarkup({
  field,
  value,
  onChange,
}: {
  field: InterviewTemplateField;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const label = `${field.label}${field.required ? " *" : ""}`;
  if (field.type === "textarea") {
    return (
      <Field label={label} hint={field.help}>
        <Textarea value={String(value ?? "")} onChange={(event) => onChange(event.target.value)} />
      </Field>
    );
  }
  if (field.type === "checkbox") {
    return (
      <Field label={label} hint={field.help}>
        <Checkbox checked={Boolean(value)} onChange={(event) => onChange(event.target.checked)} />
      </Field>
    );
  }
  if (field.type === "select" || field.type === "multiselect") {
    return (
      <Field label={label} hint={field.help}>
        <Select
          multiple={field.type === "multiselect"}
          value={field.type === "multiselect" ? ((value as string[]) ?? []) : String(value ?? "")}
          onChange={(event) =>
            onChange(
              field.type === "multiselect"
                ? Array.from(event.target.selectedOptions).map((option) => option.value)
                : event.target.value,
            )
          }
        >
          <option value="">—</option>
          {(field.options ?? []).map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </Select>
      </Field>
    );
  }
  const inputType =
    field.type === "datetime"
      ? "datetime-local"
      : field.type === "date"
        ? "date"
        : field.type === "time"
          ? "time"
          : field.type === "number"
            ? "number"
            : field.type === "email"
              ? "email"
              : field.type === "url"
                ? "url"
                : "text";
  return (
    <Field label={label} hint={field.help}>
      <Input
        type={inputType}
        value={String(value ?? "")}
        onChange={(event) => onChange(field.type === "number" ? Number(event.target.value) : event.target.value)}
      />
    </Field>
  );
}
