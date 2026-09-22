import { Download, Eye } from "lucide-react";
import { t } from "../i18n";
import { fileDownloadUrl, formatBytes, openPreview, downloadUrl, formatDateTime } from "../lib/format";
import type { FileArtifact } from "../types";
import { PDF_KINDS } from "../lib/format";
import { cn } from "../lib/utils";

/** RES-2: three file chips per doc set with Preview + Download. */
export function FileChips({
  files,
  expiredAt,
  disabled,
  className,
}: {
  files: FileArtifact[];
  expiredAt?: string | null;
  disabled?: boolean;
  className?: string;
}) {
  if (expiredAt) {
    return (
      <span className="text-xs text-muted-foreground" title={t("resumes.detail.expires", { date: formatDateTime(expiredAt) })}>
        {t("resumes.filterStatus.expired")}
      </span>
    );
  }
  const available = PDF_KINDS.map((kind) => files.find((file) => file.kind === kind)).filter(
    (file): file is FileArtifact => Boolean(file),
  );
  if (!available.length) {
    return <span className="text-xs text-muted-foreground">—</span>;
  }
  return (
    <span className={cn("flex flex-wrap items-center gap-1", className)}>
      {available.map((file) => {
        const url = fileDownloadUrl(file.id);
        const canPreview = file.kind !== "docx";
        return (
          <span
            key={file.id}
            className="inline-flex items-center overflow-hidden rounded border border-border text-[11px]"
            title={`${file.filename} · ${formatBytes(file.size_bytes)}`}
          >
            <span className="bg-muted px-1.5 py-0.5 font-medium">{file.kind.toUpperCase()}</span>
            {canPreview ? (
              <button
                type="button"
                disabled={disabled}
                onClick={() => openPreview(fileDownloadUrl(file.id, true))}
                className="px-1 py-0.5 hover:bg-accent disabled:opacity-40"
                aria-label={`${t("common.preview")} ${file.kind}`}
              >
                <Eye className="h-3 w-3" />
              </button>
            ) : null}
            <button
              type="button"
              disabled={disabled}
              onClick={() => downloadUrl(url)}
              className="px-1 py-0.5 hover:bg-accent disabled:opacity-40"
              aria-label={`${t("common.download")} ${file.kind}`}
            >
              <Download className="h-3 w-3" />
            </button>
          </span>
        );
      })}
    </span>
  );
}
