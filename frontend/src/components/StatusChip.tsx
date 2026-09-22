import { Badge } from "../ui/primitives";
import { t } from "../i18n";
import type { DerivedStatus, Role } from "../types";
import { formatSeq } from "../lib/format";

export function statusTone(status?: string): "neutral" | "success" | "warning" | "danger" | "info" {
  switch (status) {
    case "released":
    case "ready":
      return "success";
    case "retry_wait":
    case "waiting":
    case "rendering":
      return "warning";
    case "needs_attention":
      return "danger";
    case "skipped":
    case "cancelled":
      return "neutral";
    default:
      return "info";
  }
}

/** RES-4 / ORD-6: the Maker-facing wording of the derived state. */
export function statusLabel(status: DerivedStatus | undefined, role: Role): string {
  if (!status) return "—";
  if (status.status === "needs_attention") {
    return role === "manager" ? t("resumes.filterStatus.attention") : t("resumes.waitingForManager");
  }
  if (status.status === "released" && status.released_late) return t("resumes.releasedLate");
  if (status.status === "waiting" && status.blocker_seq) {
    const seq = formatSeq(status.blocker_seq);
    if (status.blocker_state === "needs_attention") return t("resumes.waitingBlockedAttention", { seq });
    if (status.blocker_state === "retry_wait") {
      return t("resumes.waitingBlockedRetry", { seq, attempt: status.blocker_attempt ?? 1 });
    }
    return t("resumes.waitingBlocked", { seq });
  }
  return status.label;
}

export function StatusChip({ status, role }: { status?: DerivedStatus; role: Role }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <Badge tone={statusTone(status?.status)} title={status?.error ?? undefined}>
        {statusLabel(status, role)}
      </Badge>
      {status?.status === "released" && status.released_late ? (
        <Badge tone="warning" title={t("resumes.releasedLate")}>
          !
        </Badge>
      ) : null}
    </span>
  );
}
