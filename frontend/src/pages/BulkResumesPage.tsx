import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { FileStack, Upload } from "lucide-react";
import { api, errorMessage } from "../lib/api";
import { formatDateTime } from "../lib/format";
import { t } from "../i18n";
import { Badge, Button, Card, CardHeader, Checkbox, EmptyState, ErrorNote, Field, InfoNote, Input, Spinner } from "../ui/primitives";
import { Dialog } from "../ui/dialog";
import { Table } from "../ui/table";
import { useToast } from "../ui/toast";
import { GroupChip } from "../components/GroupChip";
import type { BulkBatch, BulkSummary, Profile, ProfileGroup } from "../types";

/**
 * BULK-1: upload a CSV of "Job Links" + "JD", choose a group or some profiles and
 * a number N; the first N usable rows are generated for every chosen profile.
 */
export function BulkResumesPage() {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [batch, setBatch] = useState<BulkBatch | null>(null);

  const history = useQuery({
    queryKey: ["bulk-batches"],
    queryFn: () => api.get<{ items: BulkBatch[] }>("/bulk-resumes"),
  });

  const upload = async (file: File) => {
    setUploading(true);
    setError(null);
    try {
      const form = new FormData();
      form.append("file", file);
      const created = await api.post<BulkBatch>("/bulk-resumes/upload", form);
      void queryClient.invalidateQueries({ queryKey: ["bulk-batches"] });
      if (created.usable_rows === 0) {
        setError(t("bulk.noUsableRows", { rejected: created.rejected_rows }));
      } else {
        setBatch(created);
      }
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const items = history.data?.items ?? [];

  return (
    <div className="space-y-4">
      <div>
        <h1 className="rf-page-title">{t("bulk.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("bulk.subtitle")}</p>
      </div>

      <Card>
        <CardHeader title={t("bulk.uploadTitle")} description={t("bulk.uploadHint")} />
        <input
          ref={fileRef}
          type="file"
          accept=".csv,text/csv"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void upload(file);
          }}
        />
        <Button onClick={() => fileRef.current?.click()} loading={uploading}>
          <Upload className="h-4 w-4" />
          {t("bulk.uploadButton")}
        </Button>
        {error ? (
          <div className="mt-3">
            <ErrorNote>{error}</ErrorNote>
          </div>
        ) : null}
      </Card>

      <Card>
        <CardHeader title={t("bulk.history")} />
        {history.isLoading ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner /> {t("common.loading")}
          </p>
        ) : items.length === 0 ? (
          <EmptyState title={t("bulk.historyEmpty")} />
        ) : (
          <Table>
            <thead>
              <tr>
                <th>{t("bulk.file")}</th>
                <th>{t("bulk.uploaded")}</th>
                <th>{t("bulk.rows")}</th>
                <th>{t("bulk.result")}</th>
                <th className="w-10" />
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td className="max-w-[16rem] truncate">{item.filename ?? "—"}</td>
                  <td className="whitespace-nowrap text-xs">{formatDateTime(item.created_at)}</td>
                  <td className="text-xs">
                    {t("bulk.rowsSummary", { usable: item.usable_rows, rejected: item.rejected_rows })}
                  </td>
                  <td className="text-xs">
                    {item.summary
                      ? t("bulk.resultSummary", { created: item.summary.created, profiles: item.summary.profiles.length })
                      : <Badge tone="outline">{t("bulk.notGenerated")}</Badge>}
                  </td>
                  <td>
                    {!item.generated_at && item.usable_rows > 0 ? (
                      <Button size="sm" variant="outline" onClick={() => setBatch(item)}>
                        {t("bulk.continue")}
                      </Button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      {batch ? (
        <BulkDialog
          batch={batch}
          onClose={() => setBatch(null)}
          onGenerated={(summary) => {
            push({ tone: "success", title: t("bulk.generated", { count: summary.created }) });
            void queryClient.invalidateQueries({ queryKey: ["bulk-batches"] });
            void queryClient.invalidateQueries({ queryKey: ["doc-sets"] });
          }}
        />
      ) : null}
    </div>
  );
}

function BulkDialog({
  batch,
  onClose,
  onGenerated,
}: {
  batch: BulkBatch;
  onClose: () => void;
  onGenerated: (summary: BulkSummary) => void;
}) {
  const navigate = useNavigate();
  const [step, setStep] = useState<"choose" | "count" | "done">("choose");
  const [mode, setMode] = useState<"group" | "profiles">("group");
  const [groupId, setGroupId] = useState<string>("");
  const [profileIds, setProfileIds] = useState<Set<string>>(new Set());
  const [count, setCount] = useState(Math.min(10, batch.usable_rows));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<BulkSummary | null>(null);

  const groups = useQuery({
    queryKey: ["profile-groups"],
    queryFn: () => api.get<{ items: ProfileGroup[] }>("/profile-groups"),
  });
  const profiles = useQuery({
    queryKey: ["profiles", { status: "active" }],
    queryFn: () => api.get<{ items: Profile[] }>("/profiles", { status: "active" }),
  });

  const groupList = groups.data?.items ?? [];
  const profileList = profiles.data?.items ?? [];
  const chosenProfiles =
    mode === "group"
      ? profileList.filter((profile) => profile.group?.id === groupId)
      : profileList.filter((profile) => profileIds.has(profile.id));
  const withMaker = chosenProfiles.filter((profile) => profile.maker_count > 0).length;
  const canNext = mode === "group" ? Boolean(groupId) : profileIds.size > 0;
  const validCount = Number.isInteger(count) && count >= 1 && count <= batch.usable_rows;

  const toggleProfile = (profileId: string) =>
    setProfileIds((current) => {
      const next = new Set(current);
      if (next.has(profileId)) next.delete(profileId);
      else next.add(profileId);
      return next;
    });

  const generate = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await api.post<BulkSummary>(`/bulk-resumes/${batch.id}/generate`, {
        ...(mode === "group" ? { group_id: groupId } : { profile_ids: [...profileIds] }),
        count,
      });
      setSummary(result);
      setStep("done");
      onGenerated(result);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const rowsNote = (
    <InfoNote>
      {t("bulk.rowsNote", { usable: batch.usable_rows, rejected: batch.rejected_rows })}
      {batch.rejected.length ? (
        <span className="mt-1 block text-xs">
          {batch.rejected
            .slice(0, 5)
            .map((item) => t("bulk.rejectedRow", { row: item.row, reason: item.reason }))
            .join(" · ")}
          {batch.rejected_rows > 5 ? " …" : ""}
        </span>
      ) : null}
    </InfoNote>
  );

  const footer =
    step === "choose" ? (
      <>
        <Button variant="outline" onClick={onClose}>
          {t("common.cancel")}
        </Button>
        <Button onClick={() => setStep("count")} disabled={!canNext}>
          {t("bulk.next")}
        </Button>
      </>
    ) : step === "count" ? (
      <>
        <Button variant="outline" onClick={() => setStep("choose")} disabled={busy}>
          {t("bulk.back")}
        </Button>
        <Button onClick={() => void generate()} loading={busy} disabled={!validCount || withMaker === 0}>
          <FileStack className="h-4 w-4" />
          {t("bulk.generate")}
        </Button>
      </>
    ) : (
      <>
        <Button variant="outline" onClick={onClose}>
          {t("common.close")}
        </Button>
        <Button onClick={() => navigate("/resumes")}>{t("bulk.goToResumes")}</Button>
      </>
    );

  return (
    <Dialog
      open
      onClose={busy ? () => undefined : onClose}
      width="max-w-2xl"
      title={step === "count" ? t("bulk.countTitle") : step === "done" ? t("bulk.doneTitle") : t("bulk.chooseTitle")}
      description={batch.filename ?? undefined}
      footer={footer}
    >
      <div className="space-y-3">
        {step === "choose" ? (
          <>
            {rowsNote}
            <div className="flex gap-2">
              <Button size="sm" variant={mode === "group" ? "primary" : "outline"} onClick={() => setMode("group")}>
                {t("bulk.byGroup")}
              </Button>
              <Button size="sm" variant={mode === "profiles" ? "primary" : "outline"} onClick={() => setMode("profiles")}>
                {t("bulk.byProfiles")}
              </Button>
            </div>
            {groups.isLoading || profiles.isLoading ? (
              <p className="flex items-center gap-2 text-sm text-muted-foreground">
                <Spinner /> {t("common.loading")}
              </p>
            ) : mode === "group" ? (
              groupList.length === 0 ? (
                <p className="text-sm text-muted-foreground">{t("bulk.noGroups")}</p>
              ) : (
                <ul className="max-h-80 divide-y divide-border overflow-y-auto rounded-md border border-border">
                  {groupList.map((group) => (
                    <li key={group.id}>
                      <label className="flex cursor-pointer items-center gap-3 px-3 py-2 text-sm hover:bg-accent/40">
                        <input
                          type="radio"
                          name="bulk-group"
                          checked={groupId === group.id}
                          onChange={() => setGroupId(group.id)}
                        />
                        <GroupChip group={group} />
                        <span className="text-xs text-muted-foreground">
                          {t("profiles.groups.count", { count: group.profile_count })}
                        </span>
                      </label>
                    </li>
                  ))}
                </ul>
              )
            ) : (
              <ul className="max-h-80 divide-y divide-border overflow-y-auto rounded-md border border-border">
                {profileList.map((profile) => (
                  <li key={profile.id}>
                    <label className="flex cursor-pointer items-center gap-3 px-3 py-2 text-sm hover:bg-accent/40">
                      <Checkbox checked={profileIds.has(profile.id)} onChange={() => toggleProfile(profile.id)} />
                      <span className="flex-1">{profile.name}</span>
                      {profile.group ? <GroupChip group={profile.group} /> : null}
                      {profile.maker_count === 0 ? <Badge tone="warning">{t("bulk.noMaker")}</Badge> : null}
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : null}

        {step === "count" ? (
          <>
            {rowsNote}
            <Field label={t("bulk.countLabel")} hint={t("bulk.countHint", { max: batch.usable_rows })}>
              <Input
                type="number"
                min={1}
                max={batch.usable_rows}
                value={Number.isNaN(count) ? "" : String(count)}
                onChange={(event) => setCount(Number.parseInt(event.target.value, 10))}
              />
            </Field>
            <InfoNote>
              {t("bulk.countPreview", {
                count: validCount ? count : "—",
                profiles: withMaker,
                total: validCount ? count * withMaker : "—",
              })}
              {chosenProfiles.length > withMaker ? (
                <span className="mt-1 block text-xs">
                  {t("bulk.noMakerSkipped", { count: chosenProfiles.length - withMaker })}
                </span>
              ) : null}
            </InfoNote>
          </>
        ) : null}

        {step === "done" && summary ? <SummaryView summary={summary} /> : null}
        {error ? <ErrorNote>{error}</ErrorNote> : null}
      </div>
    </Dialog>
  );
}

function SummaryView({ summary }: { summary: BulkSummary }) {
  return (
    <div className="space-y-3 text-sm">
      <p className="font-medium">{t("bulk.generated", { count: summary.created })}</p>
      <ul className="divide-y divide-border rounded-md border border-border">
        {summary.profiles.map((item) => (
          <li key={item.profile_id} className="flex flex-wrap items-center gap-2 px-3 py-2">
            <span className="flex-1">
              {item.profile_name} <span className="text-muted-foreground">· {item.maker_name}</span>
            </span>
            <Badge tone="success">{t("bulk.createdN", { count: item.created })}</Badge>
            {item.skipped_duplicates ? <Badge tone="outline">{t("bulk.duplicatesN", { count: item.skipped_duplicates })}</Badge> : null}
            {item.short_by ? <Badge tone="warning">{t("bulk.shortBy", { count: item.short_by })}</Badge> : null}
          </li>
        ))}
        {summary.skipped_profiles.map((item) => (
          <li key={item.profile_id} className="flex items-center gap-2 px-3 py-2 text-muted-foreground">
            <span className="flex-1">{item.profile_name}</span>
            <Badge tone="warning">{item.reason === "no maker assigned" ? t("bulk.noMaker") : item.reason}</Badge>
          </li>
        ))}
      </ul>
      {summary.csv_duplicates || summary.rejected_rows ? (
        <p className="text-xs text-muted-foreground">
          {t("bulk.fileSkips", { duplicates: summary.csv_duplicates, rejected: summary.rejected_rows })}
        </p>
      ) : null}
    </div>
  );
}
