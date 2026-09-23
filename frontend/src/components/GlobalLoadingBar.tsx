import { useIsFetching, useIsMutating } from "@tanstack/react-query";

/**
 * UI-8: a slim top bar whenever the app is talking to the API, so clicking a
 * button, changing a filter or receiving an SSE refresh always shows motion.
 */
export function GlobalLoadingBar() {
  const fetching = useIsFetching();
  const mutating = useIsMutating();
  const busy = fetching + mutating > 0;
  return (
    <div
      aria-hidden={!busy}
      className={
        "pointer-events-none fixed inset-x-0 top-0 z-50 h-0.5 overflow-hidden transition-opacity duration-150 " +
        (busy ? "opacity-100" : "opacity-0")
      }
    >
      <div className="h-full w-1/3 animate-loading-bar rounded-full bg-primary" />
    </div>
  );
}
