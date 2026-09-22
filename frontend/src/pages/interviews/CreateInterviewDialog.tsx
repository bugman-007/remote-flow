import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Paperclip } from "lucide-react";
import { api, errorMessage } from "../../lib/api";
import { Dialog } from "../../ui/dialog";
import { Button, ErrorNote, Field, Input, Select } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import { t } from "../../i18n";
import { todayISO } from "../../lib/format";
import type { InterviewStatus, InterviewStep, Paginated, User } from "../../types";

/**
 * INT-16: a hand-made interview for candidates without a generated doc set.
 * The resume and JD are stored as-is — no LLM/render pipeline runs.
 */
export function CreateInterviewDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const { push } = useToast();
  const [company, setCompany] = useState("");
  const [jobTitle, setJobTitle] = useState("");
  const [candidate, setCandidate] = useState("");
  const [techStack, setTechStack] = useState("");
  const [meetingDate, setMeetingDate] = useState(todayISO());
  const [startTime, setStartTime] = useState("");
  const [reviewerId, setReviewerId] = useState("");
  const [stepId, setStepId] = useState("");
  const [statusId, setStatusId] = useState("");
  const [resume, setResume] = useState<File | null>(null);
  const [jd, setJd] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reviewers = useQuery({
    queryKey: ["users", { role: "reviewer" }],
    queryFn: () => api.get<Paginated<User>>("/users", { role: "reviewer", page_size: 200 }),
    enabled: open,
  });
  const taxonomy = useQuery({
    queryKey: ["interview-taxonomy"],
    queryFn: () => api.get<{ steps: InterviewStep[]; statuses: InterviewStatus[] }>("/interview-taxonomy"),
    enabled: open,
  });

  const upload = async (kind: "resume" | "jd", file: File) => {
    const body = new FormData();
    body.append("kind", kind);
    body.append("file", file);
    return api.post<{ id: string }>("/interviews/attachments", body);
  };

  const submit = async () => {
    if (!company.trim() || !jobTitle.trim()) {
      setError(t("interviews.createInterviewHint"));
      return;
    }
    if (!resume || !jd) {
      setError(t("interviews.resumeRequired"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const [resumeFile, jdFile] = await Promise.all([upload("resume", resume), upload("jd", jd)]);
      await api.post("/interviews", {
        doc_set_id: null,
        company_name: company.trim(),
        job_title: jobTitle.trim(),
        candidate_name: candidate.trim() || null,
        tech_stack: techStack.trim() || null,
        reviewer_id: reviewerId || null,
        step_id: stepId || null,
        status_id: statusId || null,
        meeting_at: startTime ? `${meetingDate}T${startTime.length === 5 ? `${startTime}:00` : startTime}` : null,
        meeting_tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
        attachment_ids: [resumeFile.id, jdFile.id],
      });
      push({ tone: "success", title: t("interviews.createInterviewSaved") });
      setCompany("");
      setJobTitle("");
      setCandidate("");
      setTechStack("");
      setStartTime("");
      setReviewerId("");
      setStepId("");
      setStatusId("");
      setResume(null);
      setJd(null);
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
      title={t("interviews.createNew")}
      description={t("interviews.createInterviewHint")}
      width="max-w-2xl"
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            {t("common.cancel")}
          </Button>
          <Button onClick={() => void submit()} loading={busy}>
            {t("common.create")}
          </Button>
        </>
      }
    >
      <div className="grid gap-3 tablet:grid-cols-2">
        <Field label={`${t("resumes.columns.company")} *`}>
          <Input value={company} onChange={(event) => setCompany(event.target.value)} />
        </Field>
        <Field label={`${t("resumes.columns.title")} *`}>
          <Input value={jobTitle} onChange={(event) => setJobTitle(event.target.value)} />
        </Field>
        <Field label={t("interviews.candidateName")}>
          <Input value={candidate} onChange={(event) => setCandidate(event.target.value)} />
        </Field>
        <Field label={t("interviews.techStack")} hint={t("interviews.techStackHint")}>
          <Input value={techStack} onChange={(event) => setTechStack(event.target.value)} />
        </Field>
      </div>

      <div className="mt-3 grid gap-3 tablet:grid-cols-3">
        <Field label={t("interviews.meetingDate")}>
          <Input type="date" value={meetingDate} onChange={(event) => setMeetingDate(event.target.value)} />
        </Field>
        <Field label={t("interviews.startTime")}>
          <Input type="time" value={startTime} onChange={(event) => setStartTime(event.target.value)} />
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
      </div>

      <div className="mt-3 grid gap-3 tablet:grid-cols-2">
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
        <Field label={t("interviews.statusLabel")}>
          <Select value={statusId} onChange={(event) => setStatusId(event.target.value)}>
            <option value="">—</option>
            {(taxonomy.data?.statuses ?? []).map((status) => (
              <option key={status.id} value={status.id}>
                {status.name}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      <div className="mt-3 grid gap-3 tablet:grid-cols-2">
        <Field label={t("interviews.attachResume")} hint={t("interviews.attachHint")}>
          <Input type="file" onChange={(event) => setResume(event.target.files?.[0] ?? null)} />
          {resume ? <p className="mt-1 text-xs text-muted-foreground"><Paperclip className="mr-1 inline h-3 w-3" />{resume.name}</p> : null}
        </Field>
        <Field label={t("interviews.attachJd")} hint={t("interviews.attachHint")}>
          <Input type="file" onChange={(event) => setJd(event.target.files?.[0] ?? null)} />
          {jd ? <p className="mt-1 text-xs text-muted-foreground"><Paperclip className="mr-1 inline h-3 w-3" />{jd.name}</p> : null}
        </Field>
      </div>

      {error ? (
        <div className="mt-3">
          <ErrorNote>{error}</ErrorNote>
        </div>
      ) : null}
    </Dialog>
  );
}
