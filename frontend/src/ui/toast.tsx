import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { X } from "lucide-react";

export interface Toast {
  id: number;
  title: string;
  description?: string;
  tone: "info" | "success" | "error";
  href?: string;
  hrefLabel?: string;
}

interface ToastContextValue {
  push: (toast: Omit<Toast, "id">) => void;
}

const ToastContext = createContext<ToastContextValue>({ push: () => undefined });

export function useToast(): ToastContextValue {
  return useContext(ToastContext);
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const push = useCallback((toast: Omit<Toast, "id">) => {
    const id = Date.now() + Math.random();
    setToasts((current) => [...current, { ...toast, id }]);
    window.setTimeout(() => {
      setToasts((current) => current.filter((item) => item.id !== id));
    }, toast.tone === "error" ? 9000 : 5500);
  }, []);

  const value = useMemo(() => ({ push }), [push]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-80 flex-col gap-2">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={
              "pointer-events-auto rounded-md border px-3 py-2 shadow-lg " +
              (toast.tone === "error"
                ? "border-destructive/40 bg-destructive/10 text-destructive"
                : toast.tone === "success"
                  ? "border-emerald-500/40 bg-emerald-500/10"
                  : "border-border bg-card")
            }
            role="status"
          >
            <div className="flex items-start justify-between gap-2">
              <div>
                <p className="text-sm font-medium">{toast.title}</p>
                {toast.description ? <p className="mt-0.5 text-xs text-muted-foreground">{toast.description}</p> : null}
                {toast.href ? (
                  <a className="mt-1 inline-block text-xs font-medium underline" href={toast.href}>
                    {toast.hrefLabel ?? toast.href}
                  </a>
                ) : null}
              </div>
              <button
                type="button"
                aria-label="Dismiss"
                className="opacity-60 hover:opacity-100"
                onClick={() => setToasts((current) => current.filter((item) => item.id !== toast.id))}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
