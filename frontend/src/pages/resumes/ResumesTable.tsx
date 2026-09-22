import { useEffect, useState } from "react";
import { Download, Eye, ListTree, Pencil, Play, RefreshCw, SkipForward, Sparkles, Star } from "lucide-react";
import { api, downloadBlob, errorMessage } from "../../lib/api";
import { formatDateTime, formatTime, formatSeq } from "../../lib/format";
import { Badge, Button, Checkbox, Input } from "../../ui/primitives";
import { Table, TableState } from "../../ui/table";
import { DropdownMenu, MenuItem, MenuSeparator } from "../../ui/menu";
import { Dialog } from "../../ui/dialog";
import { FileChips } from "../../components/FileChips";
import { StatusChip } from "../../components/StatusChip";
import { t } from "../../i18n";
import type { DocSetRow, Pagination, Role } from "../../types";

interface Props {
  rows: DocSetRow[];
  pagination?: Pagination;
  loading: boolean;
  error?: unknown;
  role: Role;
  selection: string[];
  onSelectionChange: (ids: string[]) => void;
  onOpen: (row: DocSetRow) => void;
  onChanged: () => void;
  showSelection: boolean;
  sort: { key: string; dir: "asc" | "desc" };
  onSort: (key: string) => void;
}

export function ResumesTable({
  rows,
  loading,
  error,
  role,
  selection,
  onSelectionChange,
  onOpen,
  onChanged,
  showSelection,
  sort,
  onSort,
}: Props) {
  const [renaming, setRenaming] = useState<DocSetRow | null>(null);
  const [regenerating, setRegenerating] = useState<DocSetRow | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const isManager = role === "manager";
  const columns = isManager ? 9 : 7;

  const toggle = (id: string) => {
    onSelectionChange(selection.includes(id) ? selection.filter((value) => value !== id) : [...selection, id]);
  };

  const act = async (fn: () => Promise<unknown>, success?: string) => {
    setBusy(true);
    setNotice(null);
    try {
      await fn();
      if (success) setNotice(success);
      onChanged();
    } catch (caught) {
      const message = errorMessage(caught);
      setNotice(message);
      if (message.includes("interview")) window.alert(message);
    } finally {
      setBusy(false);
    }
  };

  const sortArrow = (key: string) => (sort.key === key ? (sort.dir === "asc" ? " ↑" : " ↓") : "");

  return (
    <>
      <Table>
        <thead>
          <tr>
            {showSelection ? (
              <th className="w-8">
                <Checkbox
                  aria-label={t("common.selectAll")}
                  checked={rows.length > 0 && rows.every((row) => selection.includes(row.doc_set_id))}
                  onChange={(event) => onSelectionChange(event.target.checked ? rows.map((row) => row.doc_set_id) : [])}
                />
              </th>
            ) : null}
            <th className="cursor-pointer" onClick={() => onSort("seq")}>
              {t("resumes.columns.seq")}
              {sortArrow("seq")}
            </th>
            <th className="cursor-pointer" onClick={() => onSort("company")}>
              {t("resumes.columns.company")}
              {sortArrow("company")}
            </th>
            <th>{t("resumes.columns.title")}</th>
            {isManager ? <th>{t("resumes.columns.maker")}</th> : null}
            <th className="cursor-pointer" onClick={() => onSort("status")}>
              {t("resumes.columns.status")}
              {sortArrow("status")}
            </th>
            <th>{t("resumes.columns.submitted")}</th>
            <th className="cursor-pointer" onClick={() => onSort("ready")}>
              {t("resumes.columns.ready")}
              {sortArrow("ready")}
            </th>
            <th>{t("resumes.columns.files")}</th>
            <th className="w-10" />
          </tr>
        </thead>
        <tbody>
          <TableState
            loading={loading}
            error={error ? errorMessage(error) : undefined}
            empty={t("common.noResults")}
            colSpan={columns + 1}
          />
          {!loading && !error
            ? rows.map((row) => {
                const ready = row.status?.status === "released";
                return (
                  <tr key={row.id} className="hover:bg-accent/40">
                    {showSelection ? (
                      <td>
                        <Checkbox
                          aria-label={t("resumes.select")}
                          checked={selection.includes(row.doc_set_id)}
                          onChange={() => toggle(row.doc_set_id)}
                          disabled={!isManager}
                        />
                      </td>
                    ) : null}
                    <td className="font-mono text-xs">{formatSeq(row.seq_no)}</td>
                    <td className="max-w-[14rem] truncate">
                      <button type="button" className="text-left hover:underline" onClick={() => onOpen(row)}>
                        {row.doc_set?.company_name ?? t("common.unknown")}
                      </button>
                      {row.duplicate_of ? (
                        <Badge tone="warning" className="ml-1">
                          {t("jd.duplicateBadge", { seq: formatSeq(row.duplicate_seq ?? 0) })}
                        </Badge>
                      ) : null}
                      {row.generation_count > 1 ? (
                        <Badge tone="outline" className="ml-1">
                          {t("resumes.generatedBadge", { n: row.generation_count })}
                        </Badge>
                      ) : null}
                    </td>
                    <td className="max-w-[16rem] truncate">{row.doc_set?.job_title ?? "—"}</td>
                    {isManager ? <td className="max-w-[10rem] truncate">{row.maker_name ?? "—"}</td> : null}
                    <td>
                      <StatusChip status={row.status} role={role} />
                      {row.is_selected || row.keep ? (
                        <span className="ml-1 inline-flex gap-1">
                          {row.is_selected ? (
                            <Badge tone="info" title={t("resumes.filterStatus.selected")}>
                              <Star className="h-3 w-3" />
                            </Badge>
                          ) : null}
                          {row.keep ? <Badge tone="outline">K</Badge> : null}
                        </span>
                      ) : null}
                    </td>
                    <td className="whitespace-nowrap text-xs text-muted-foreground" title={formatDateTime(row.submitted_at)}>
                      {formatTime(row.submitted_at)}
                    </td>
                    <td className="whitespace-nowrap text-xs text-muted-foreground">{formatTime(row.released_at)}</td>
                    <td>
                      <FileChips files={row.files} expiredAt={row.expired ? row.doc_set?.files_expired_at : null} disabled={!ready} />
                    </td>
                    <td>
                      <DropdownMenu>
                        {(close) => (
                          <>
                            <MenuItem
                              onSelect={() => {
                                close();
                                onOpen(row);
                              }}
                            >
                              <ListTree className="mr-2 inline h-3.5 w-3.5" />
                              {t("resumes.details")}
                            </MenuItem>
                            <MenuItem
                              onSelect={() => {
                                close();
                                if (row.doc_set_id) void act(() => api.blob(`/doc-sets/${row.doc_set_id}/zip`).then((blob) => downloadBlob(blob)), t("toast.downloaded"));
                              }}
                            >
                              <Download className="mr-2 inline h-3.5 w-3.5" />
                              {t("resumes.downloadSet")}
                            </MenuItem>
                            {isManager ? <MenuSeparator /> : null}
                            {isManager ? (
                              <MenuItem
                                onSelect={() => {
                                  close();
                                  setRenaming(row);
                                }}
                              >
                                <Pencil className="mr-2 inline h-3.5 w-3.5" />
                                {t("resumes.rename")}
                              </MenuItem>
                            ) : null}
                            {isManager ? (
                              <>
                                <MenuItem
                                  onSelect={() => {
                                    close();
                                    void act(() => api.patch(`/doc-sets/${row.doc_set_id}`, { keep: !row.keep }));
                                  }}
                                >
                                  {row.keep ? t("resumes.unkeep") : t("resumes.keep")}
                                </MenuItem>
                                <MenuItem
                                  onSelect={() => {
                                    close();
                                    void act(() => api.post(`/jobs/${row.id}/retry`, { mode: "render_only" }), t("toast.retried"));
                                  }}
                                >
                                  <Play className="mr-2 inline h-3.5 w-3.5" />
                                  {t("resumes.retryNow")} · {t("resumes.retryRenderOnly")}
                                </MenuItem>
                                <MenuItem
                                  onSelect={() => {
                                    close();
                                    void act(() => api.post(`/jobs/${row.id}/retry`, { mode: "new_llm_call" }), t("toast.retried"));
                                  }}
                                >
                                  <RefreshCw className="mr-2 inline h-3.5 w-3.5" />
                                  {t("resumes.retryNow")} · {t("resumes.retryNewLlm")}
                                </MenuItem>
                                <MenuItem
                                  onSelect={() => {
                                    close();
                                    if (window.confirm(t("confirm.skip", { seq: formatSeq(row.seq_no) }))) {
                                      void act(() => api.post(`/jobs/${row.id}/skip`), t("toast.skipped", { seq: formatSeq(row.seq_no) }));
                                    }
                                  }}
                                >
                                  <SkipForward className="mr-2 inline h-3.5 w-3.5" />
                                  {t("resumes.skip")}
                                </MenuItem>
                                <MenuSeparator />
                                <MenuItem
                                  onSelect={() => {
                                    close();
                                    setRegenerating(row);
                                  }}
                                >
                                  <Sparkles className="mr-2 inline h-3.5 w-3.5" />
                                  {t("resumes.regenerate")}
                                </MenuItem>
                                <MenuItem
                                  onSelect={() => {
                                    close();
                                    onOpen(row);
                                  }}
                                >
                                  <Eye className="mr-2 inline h-3.5 w-3.5" />
                                  {t("resumes.viewLlmJson")}
                                </MenuItem>
                              </>
                            ) : null}
                          </>
                        )}
                      </DropdownMenu>
                    </td>
                  </tr>
                );
              })
            : null}
        </tbody>
      </Table>
      {notice ? <p className="mt-2 text-xs text-muted-foreground">{notice}</p> : null}

      <RenameDialog
        row={renaming}
        busy={busy}
        onClose={() => setRenaming(null)}
        onSave={async (values) => {
          if (!renaming) return;
          await act(() => api.patch(`/doc-sets/${renaming.doc_set_id}`, values), t("toast.saved"));
          setRenaming(null);
        }}
      />

      <Dialog
        open={Boolean(regenerating)}
        onClose={() => setRegenerating(null)}
        title={t("resumes.regenerate")}
        width="max-w-md"
        footer={
          <>
            <Button variant="outline" onClick={() => setRegenerating(null)} disabled={busy}>
              {t("common.cancel")}
            </Button>
            <Button
              disabled={busy}
              onClick={() => {
                if (!regenerating) return;
                void act(() => api.post(`/doc-sets/${regenerating.doc_set_id}/regenerate`), t("toast.regenerating")).then(() =>
                  setRegenerating(null),
                );
              }}
            >
              {t("common.confirm")}
            </Button>
          </>
        }
      >
        <p className="text-sm text-muted-foreground">
          {(regenerating?.interview_count ?? 0) > 0
            ? t("resumes.regenerateConfirm", {
                count: regenerating?.interview_count ?? 0,
                generation: regenerating?.status?.generation ?? 1,
              })
            : t("resumes.regenerateConfirmNone")}
        </p>
      </Dialog>
    </>
  );
}

function RenameDialog({
  row,
  busy,
  onClose,
  onSave,
}: {
  row: DocSetRow | null;
  busy: boolean;
  onClose: () => void;
  onSave: (values: { company_name: string; job_title: string }) => Promise<void>;
}) {
  const [company, setCompany] = useState("");
  const [title, setTitle] = useState("");

  useEffect(() => {
    setCompany(row?.doc_set?.company_name ?? "");
    setTitle(row?.doc_set?.job_title ?? "");
  }, [row?.doc_set_id, row?.doc_set?.company_name, row?.doc_set?.job_title]);

  return (
    <Dialog
      open={Boolean(row)}
      onClose={onClose}
      title={t("resumes.rename")}
      width="max-w-md"
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            {t("common.cancel")}
          </Button>
          <Button disabled={busy} onClick={() => void onSave({ company_name: company, job_title: title })}>
            {busy ? t("common.saving") : t("common.save")}
          </Button>
        </>
      }
    >
      <label className="rf-label">{t("resumes.renameCompany")}</label>
      <Input value={company} onChange={(event) => setCompany(event.target.value)} />
      <label className="rf-label mt-3">{t("resumes.renameTitle")}</label>
      <Input value={title} onChange={(event) => setTitle(event.target.value)} />
    </Dialog>
  );
}
