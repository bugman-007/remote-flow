import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiBase } from "../lib/api";
import { useAuth } from "../auth/AuthProvider";

export type ConnectionState = "live" | "reconnecting" | "offline";

export interface RealtimeEvent {
  id: string;
  type: string;
  data: Record<string, unknown>;
}

type Handler = (event: RealtimeEvent) => void;

interface RealtimeContextValue {
  state: ConnectionState;
  lastEventAt: number | null;
  subscribe: (handler: Handler) => () => void;
}

const RealtimeContext = createContext<RealtimeContextValue>({
  state: "offline",
  lastEventAt: null,
  subscribe: () => () => undefined,
});

export function useRealtime(): RealtimeContextValue {
  return useContext(RealtimeContext);
}

const LAST_ID_KEY = "rf.lastEventId";
const POLL_AFTER_FAILURES = 2;
const POLL_INTERVAL_MS = 10_000;
const SILENT_REFRESH_MS = 60_000;

/** Event types that always warrant a refetch (RT-6). */
const REFETCH_PREFIXES = ["job.", "docset.", "build.", "interview.", "feedback.", "intake."];

export function RealtimeProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const [state, setState] = useState<ConnectionState>("reconnecting");
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);
  const handlers = useRef(new Set<Handler>());
  const failures = useRef(0);

  const subscribe = useCallback((handler: Handler) => {
    handlers.current.add(handler);
    return () => {
      handlers.current.delete(handler);
    };
  }, []);

  const dispatch = useCallback((event: RealtimeEvent) => {
    setLastEventAt(Date.now());
    handlers.current.forEach((handler) => handler(event));
  }, []);

  // RT-8: silent refresh of the active page every 60 s, and when the tab regains focus.
  useEffect(() => {
    if (!user) return;
    const refetch = () => {
      void queryClient.invalidateQueries({ refetchType: "active" });
    };
    const interval = window.setInterval(refetch, SILENT_REFRESH_MS);
    const onFocus = () => refetch();
    const onVisible = () => {
      if (document.visibilityState === "visible") refetch();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [user, queryClient]);

  // RT-1/RT-4: SSE with Last-Event-ID replay, reconnect backoff and a 10 s polling fallback.
  useEffect(() => {
    if (!user) {
      setState("offline");
      return;
    }
    let closed = false;
    let source: EventSource | null = null;
    let reconnectTimer = 0;
    let attempt = 0;

    const connect = () => {
      if (closed) return;
      const lastId = sessionStorage.getItem(LAST_ID_KEY);
      // EventSource cannot set headers, so the id travels as a query parameter and is
      // also replayed server-side from the Last-Event-ID header on native reconnects.
      const url = lastId
        ? `${apiBase}/events?last_event_id=${encodeURIComponent(lastId)}`
        : `${apiBase}/events`;
      source = new EventSource(url, { withCredentials: true });

      source.onopen = () => {
        attempt = 0;
        failures.current = 0;
        setState("live");
        // RT-6: events are notifications, not state — re-fetch on reconnect.
        void queryClient.invalidateQueries({ refetchType: "active" });
      };

      source.onmessage = (message) => {
        const event: RealtimeEvent = {
          id: message.lastEventId ?? "",
          type: message.type === "message" ? "message" : message.type,
          data: safeParse(message.data),
        };
        if (event.id) sessionStorage.setItem(LAST_ID_KEY, event.id);
        dispatch(event);
        bumpQueries(event);
      };

      const named: string[] = [
        "job.status",
        "job.released",
        "build.needs_attention",
        "docset.regenerated",
        "docset.selected",
        "limit.updated",
        "intake.paused",
        "intake.resumed",
        "interview.assigned",
        "interview.updated",
        "feedback.added",
        "system.notice",
      ];
      for (const type of named) {
        source.addEventListener(type, (raw) => {
          const message = raw as MessageEvent<string>;
          const event: RealtimeEvent = { id: message.lastEventId ?? "", type, data: safeParse(message.data) };
          if (event.id) sessionStorage.setItem(LAST_ID_KEY, event.id);
          dispatch(event);
          bumpQueries(event);
        });
      }

      source.onerror = () => {
        source?.close();
        source = null;
        if (closed) return;
        failures.current += 1;
        attempt += 1;
        setState(failures.current >= POLL_AFTER_FAILURES ? "offline" : "reconnecting");
        const delay = Math.min(30_000, 1000 * 2 ** Math.min(attempt, 5));
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };

    const simpleInvalidate: Record<string, string[]> = {
      "job.status": ["jobs", "doc-sets"],
      "job.released": ["jobs", "doc-sets", "stats"],
      "build.needs_attention": ["jobs", "doc-sets", "system-status"],
      "docset.regenerated": ["doc-sets", "jobs"],
      "docset.selected": ["doc-sets", "interviews", "jobs"],
      "limit.updated": ["limit", "users", "me"],
      "intake.paused": ["system-status", "settings", "jobs"],
      "intake.resumed": ["system-status", "settings", "jobs"],
      "interview.assigned": ["interviews", "interview"],
      "interview.updated": ["interviews", "interview"],
      "feedback.added": ["interviews", "interview", "stats"],
    };

    const bumpQueries = (event: RealtimeEvent) => {
      if (!REFETCH_PREFIXES.some((prefix) => event.type.startsWith(prefix))) {
        void queryClient.invalidateQueries({ refetchType: "active" });
        return;
      }
      const keys = simpleInvalidate[event.type];
      if (!keys) {
        void queryClient.invalidateQueries({ refetchType: "active" });
        return;
      }
      for (const key of keys) {
        void queryClient.invalidateQueries({ queryKey: [key] });
      }
    };

    connect();

    return () => {
      closed = true;
      window.clearTimeout(reconnectTimer);
      source?.close();
    };
  }, [user, dispatch, queryClient]);

  // RT-4: when the stream is considered unavailable, poll the active list every 10 s.
  useEffect(() => {
    if (state !== "offline" || !user) return;
    const interval = window.setInterval(() => {
      void queryClient.invalidateQueries({ refetchType: "active" });
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [state, user, queryClient]);

  const value = useMemo(() => ({ state, lastEventAt, subscribe }), [state, lastEventAt, subscribe]);
  return <RealtimeContext.Provider value={value}>{children}</RealtimeContext.Provider>;
}

function safeParse(raw: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}
