import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { queryToState, stateToQuery } from "./utils";

/**
 * UI-2: list state lives in the URL query string so views are shareable and survive a refresh.
 * Uses `replace` so typing in a filter box does not flood the history stack.
 */
export function useUrlState<T extends Record<string, unknown>>(defaults: T, options?: { push?: boolean }) {
  const [params, setParams] = useSearchParams();
  const state = useMemo(() => queryToState<T>(params, defaults), [params, defaults]);

  const update = useCallback(
    (patch: Partial<T>, opts?: { push?: boolean }) => {
      const next = { ...state, ...patch };
      const query = stateToQuery(next as Record<string, unknown>);
      setParams(query, { replace: !(opts?.push ?? options?.push ?? false) });
    },
    [state, setParams, options?.push],
  );

  const reset = useCallback(
    (keys?: (keyof T)[]) => {
      if (!keys) {
        setParams({}, { replace: true });
        return;
      }
      const next = { ...state };
      for (const key of keys) next[key] = defaults[key];
      setParams(stateToQuery(next as Record<string, unknown>), { replace: true });
    },
    [state, defaults, setParams],
  );

  return { state, update, reset };
}
