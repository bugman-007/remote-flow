import { useEffect, useMemo, useRef, useState } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { ClipboardPaste, Clock, Send, TriangleAlert, X } from "lucide-react";
import { api, errorMessage, isApiError } from "../lib/api";
import { formatSeq, formatTime, todayISO } from "../lib/format";
import { uuid } from "../lib/utils";
import { Badge, Button, Card, CardHeader, ErrorNote, InfoNote, Spinner } from "../ui/primitives";
import { ConfirmDialog } from "../ui/dialog";
import { useToast } from "../ui/toast";
import { StatusChip } from "../components/StatusChip";
import { useAuth } from "../auth/AuthProvider";
import { t } from "../i18n";
import type { JobRow, Paginated } from "../types";

interface LimitInfo {
  daily_limit: number | null;
  used: number;
  remaining: number | null;
  date: string;
  paused: boolean;
  min_jd_chars: number;
  max_jd_chars: number;
}

interface EtaInfo {
  pending_jobs: number;
  pending_for_maker: number;
  eta_seconds: number;
  mean_llm_ms: number;
  effective_provider_concurrency: number;
  estimate: boolean;
}

/** JD-1: fall back to the server defaults until ``/me/limit`` answers. */
const FALLBACK_MIN_CHARS = 50;
const FALLBACK_MAX_CHARS = 200_000;

/** Rows per request for the Recent submissions list (infinite scroll). */
const RECENT_PAGE_SIZE = 100;

export function JdUploadPage() {
  const { user } = useAuth();
  const { push } = useToast();
  const queryClient = useQueryClient();
  const boxRef = useRef<HTMLTextAreaElement | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [blockedReason, setBlockedReason] = useState<"no_profile" | "paused" | null>(null);
  const [lastSeq, setLastSeq] = useState<number | null>(null);
  const [cancelling, setCancelling] = useState<JobRow | null>(null);
  const [cancelBusy, setCancelBusy] = useState(false);
  // JD-1: one idempotency key per submission attempt; a retry after a network drop reuses it.
  const pendingKey = useRef<{ key: string; text: string } | null>(null);

  const limit = useQuery({
    queryKey: ["limit"],
    queryFn: () => api.get<LimitInfo>("/me/limit"),
    refetchInterval: 60_000,
  });

  const eta = useQuery({
    queryKey: ["me", "eta"],
    queryFn: () => api.get<EtaInfo>("/me/eta"),
    refetchInterval: 30_000,
  });

  // Every submission of the day, newest first, fetched a page at a time as the
  // list is scrolled (a maker can submit hundreds a day).
  const today = todayISO();
  const recent = useInfiniteQuery({
    queryKey: ["jobs", { date: today, scope: "recent" }],
    queryFn: ({ pageParam }) =>
      api.get<Paginated<JobRow>>("/jobs", { date: today, page: pageParam, page_size: RECENT_PAGE_SIZE }),
    initialPageParam: 1,
    getNextPageParam: (last) =>
      last.pagination.page < last.pagination.pages ? last.pagination.page + 1 : undefined,
    refetchInterval: 60_000,
  });
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = recent;
  const moreRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const marker = moreRef.current;
    if (!marker || !hasNextPage) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting) && !isFetchingNextPage) void fetchNextPage();
      },
      { rootMargin: "400px" },
    );
    observer.observe(marker);
    return () => observer.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  const minChars = limit.data?.min_jd_chars ?? FALLBACK_MIN_CHARS;
  const maxChars = limit.data?.max_jd_chars ?? FALLBACK_MAX_CHARS;
  const limitReached =
    limit.data?.daily_limit !== null && limit.data !== undefined && limit.data.used >= (limit.data.daily_limit ?? 0);
  const paused = Boolean(limit.data?.paused) || blockedReason === "paused";
  const disabled = busy || paused || blockedReason === "no_profile" || limitReached;

  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    box.style.height = "auto";
    box.style.height = `${Math.min(Math.max(box.scrollHeight, 160), 520)}px`;
  }, [text]);

  const trimmed = text.trim();
  const charState = useMemo(() => {
    if (!trimmed) return "empty" as const;
    if (trimmed.length < minChars) return "short" as const;
    if (trimmed.length > maxChars) return "long" as const;
    return "ok" as const;
  }, [trimmed, minChars, maxChars]);

  const bulkParts = useMemo(() => {
    if (!text.includes("-----")) return [];
    return text
      .split(/^\s*-{5,}\s*$/m)
      .map((part) => part.trim())
      .filter((part) => part.length >= minChars);
  }, [text, minChars]);

  const submit = async (body?: string) => {
    const payloadText = (body ?? text).trim();
    if (payloadText.length < minChars) {
      setError(t("jd.tooShort", { min: minChars }));
      return;
    }
    if (payloadText.length > maxChars) {
      setError(t("jd.tooLong", { max: maxChars.toLocaleString() }));
      return;
    }
    if (pendingKey.current && pendingKey.current.text !== payloadText) pendingKey.current = null;
    if (!pendingKey.current) pendingKey.current = { key: uuid(), text: payloadText };
    const key = pendingKey.current.key;

    setBusy(true);
    setError(null);
    try {
      const result = await api.request<{ job_id: string; seq_no: number; duplicate_of: string | null }>("/jobs", {
        method: "POST",
        body: { jd_text: payloadText },
        headers: { "Idempotency-Key": key },
      });
      pendingKey.current = null;
      setLastSeq(result.seq_no);
      push({
        tone: "success",
        title: t("jd.submitted", { seq: formatSeq(result.seq_no) }),
        description: result.duplicate_of ? t("jd.duplicateNote") : undefined,
      });
      if (!body) setText("");
      boxRef.current?.focus();
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      void queryClient.invalidateQueries({ queryKey: ["limit"] });
    } catch (caught) {
      if (isApiError(caught, "no_profile_assigned")) {
        setBlockedReason("no_profile");
      } else if (isApiError(caught, "intake_paused")) {
        setBlockedReason("paused");
      } else if (isApiError(caught, "limit_reached")) {
        void queryClient.invalidateQueries({ queryKey: ["limit"] });
      } else if (isApiError(caught, "validation_error") || isApiError(caught, "invalid_jd")) {
        setError(caught.message);
      } else {
        setError(`${errorMessage(caught)} ${pendingKey.current ? "(press Submit again to retry the same submission)" : ""}`.trim());
        return;
      }
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  /** MKR-3: a submission can be withdrawn until it is delivered. */
  const withdraw = async () => {
    if (!cancelling) return;
    setCancelBusy(true);
    try {
      await api.post(`/jobs/${cancelling.id}/cancel`);
      push({ tone: "success", title: t("jd.cancelled", { seq: formatSeq(cancelling.seq_no) }) });
      setCancelling(null);
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      void queryClient.invalidateQueries({ queryKey: ["limit"] });
      void queryClient.invalidateQueries({ queryKey: ["me", "eta"] });
    } catch (caught) {
      push({ tone: "error", title: t("jd.cancelFailed"), description: errorMessage(caught) });
    } finally {
      setCancelBusy(false);
    }
  };

  const items = useMemo(() => {
    // New submissions shift the pages while the list is open; never show a row twice.
    const seen = new Set<string>();
    return (recent.data?.pages ?? [])
      .flatMap((page) => page.items)
      .filter((job) => (seen.has(job.id) ? false : (seen.add(job.id), true)));
  }, [recent.data]);
  const total = recent.data?.pages[0]?.pagination.total ?? items.length;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="rf-page-title">{t("jd.title")}</h1>
          <p className="text-sm text-muted-foreground">{t("jd.subtitle")}</p>
        </div>
        <div className="text-right text-sm">
          <p className={limitReached ? "font-medium text-destructive" : "font-medium"}>
            {limit.data?.daily_limit === null || limit.data?.daily_limit === undefined
              ? t("jd.usageUnlimited", { used: limit.data?.used ?? 0 })
              : t("jd.usage", { used: limit.data?.used ?? 0, limit: limit.data.daily_limit })}
          </p>
          <p className="text-xs text-muted-foreground">
            {eta.data
              ? t("jd.estimate", { minutes: Math.max(1, Math.round(eta.data.eta_seconds / 60)) })
              : t("jd.estimateLoading")}
          </p>
        </div>
      </div>

      {blockedReason === "no_profile" ? (
        <ErrorNote>{t("jd.noProfile")}</ErrorNote>
      ) : paused ? (
        <ErrorNote>{t("jd.paused")}</ErrorNote>
      ) : limitReached ? (
        <ErrorNote>{t("jd.limitReached", { limit: limit.data?.daily_limit ?? 0 })}</ErrorNote>
      ) : null}

      <Card>
        <textarea
          ref={boxRef}
          value={text}
          spellCheck={false}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
              event.preventDefault();
              if (!disabled) void submit();
            }
          }}
          placeholder={t("jd.placeholder")}
          className="rf-input min-h-[220px] resize-none text-sm leading-relaxed"
          aria-label={t("jd.placeholder")}
        />
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
          <span className="text-xs text-muted-foreground">
            {t("jd.characters", { count: trimmed.length, max: maxChars.toLocaleString() })}
            {charState === "short" ? ` · ${t("jd.tooShort", { min: minChars })}` : ""}
            {charState === "long" ? ` · ${t("jd.tooLong", { max: maxChars.toLocaleString() })}` : ""}
          </span>
          <div className="flex items-center gap-2">
            {lastSeq !== null ? <Badge tone="info">{t("jd.submitted", { seq: formatSeq(lastSeq) })}</Badge> : null}
            <Button loading={busy} disabled={disabled || charState !== "ok"} onClick={() => void submit()}>
              <Send className="h-4 w-4" />
              {busy ? t("jd.submitting") : t("jd.submit")}
            </Button>
          </div>
        </div>
        {error ? (
          <div className="mt-2">
            <ErrorNote>{error}</ErrorNote>
          </div>
        ) : null}
        {bulkParts.length > 1 ? (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Badge tone="info">
              <ClipboardPaste className="h-3 w-3" />
              {t("jd.bulkDetected", { count: bulkParts.length })}
            </Badge>
            <Button
              variant="outline"
              size="sm"
              disabled={disabled}
              onClick={() => void submitBulk(bulkParts, submit, () => setText(""))}
            >
              {t("jd.bulkSplit", { count: bulkParts.length })}
            </Button>
          </div>
        ) : null}
      </Card>

      <Card>
        <CardHeader title={t("jd.recent")} description={t("jd.recentCount", { date: today, count: total })} />
        {recent.isLoading ? (
          <p className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Spinner /> {t("common.loading")}
          </p>
        ) : recent.isError ? (
          <ErrorNote>{errorMessage(recent.error)}</ErrorNote>
        ) : items.length === 0 ? (
          <InfoNote>{t("jd.recentEmpty")}</InfoNote>
        ) : (
          <ul className="divide-y divide-border">
            {items.map((job) => (
              <li key={job.id} className="flex flex-wrap items-center gap-3 py-2 text-sm">
                <span className="w-14 font-mono text-xs text-muted-foreground">{formatSeq(job.seq_no)}</span>
                <span className="min-w-0 flex-1 truncate">
                  {job.doc_set?.company_name ?? t("common.unknown")}
                  {job.doc_set?.job_title ? <span className="text-muted-foreground"> · {job.doc_set.job_title}</span> : null}
                </span>
                {job.duplicate_of ? (
                  <Badge tone="warning" title={t("jd.duplicateNote")}>
                    {t("jd.duplicateBadge", { seq: formatSeq(job.duplicate_seq ?? 0) })}
                  </Badge>
                ) : null}
                <span className="flex items-center gap-1 text-xs text-muted-foreground">
                  <Clock className="h-3 w-3" />
                  {formatTime(job.submitted_at)}
                </span>
                <StatusChip status={job.status} role={user?.role ?? "maker"} />
                {job.delivery_status === "pending" ? (
                  <Button
                    variant="ghost"
                    size="icon"
                    title={t("jd.cancelSubmission")}
                    aria-label={t("jd.cancelSubmission")}
                    onClick={() => setCancelling(job)}
                  >
                    <X className="h-3.5 w-3.5" />
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
        {hasNextPage ? (
          <div ref={moreRef} className="flex items-center justify-center gap-2 py-3 text-xs text-muted-foreground">
            {isFetchingNextPage ? <Spinner /> : null}
            {t("jd.recentMore", { shown: items.length, total })}
          </div>
        ) : null}
      </Card>

      <ConfirmDialog
        open={cancelling !== null}
        title={t("jd.cancelConfirmTitle", { seq: formatSeq(cancelling?.seq_no ?? 0) })}
        message={t("jd.cancelConfirmBody")}
        confirmLabel={t("jd.cancelConfirmLabel")}
        destructive
        busy={cancelBusy}
        onCancel={() => setCancelling(null)}
        onConfirm={() => void withdraw()}
      />

      {user?.role === "maker" && user.must_change_password ? (
        <InfoNote>
          <TriangleAlert className="mr-1 inline h-3.5 w-3.5" />
          {t("password.forced")}
        </InfoNote>
      ) : null}
    </div>
  );
}

async function submitBulk(parts: string[], submit: (body?: string) => Promise<void>, onDone: () => void): Promise<void> {
  for (const part of parts) {
    await submit(part);
  }
  onDone();
}
