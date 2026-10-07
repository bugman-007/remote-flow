import { Radio } from "lucide-react";
import { useRealtime } from "../realtime/RealtimeProvider";
import { t } from "../i18n";
import { cn } from "../lib/utils";

export function ConnectionIndicator({ compact = false }: { compact?: boolean }) {
  const { state } = useRealtime();
  const tone =
    state === "live"
      ? "text-success"
      : state === "reconnecting"
        ? "text-warning"
        : "text-destructive";
  const label =
    state === "live" ? t("connection.live") : state === "reconnecting" ? t("connection.reconnecting") : t("connection.offline");
  return (
    <span
      className={cn("inline-flex items-center gap-1.5 text-xs font-medium", tone)}
      title={state === "offline" ? "Live updates unavailable — polling every 10 s" : undefined}
    >
      <Radio className={cn("h-3.5 w-3.5", state === "live" && "animate-pulse")} />
      {compact ? null : label}
    </span>
  );
}
