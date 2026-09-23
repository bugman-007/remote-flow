import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { MoreHorizontal } from "lucide-react";
import { Button } from "./primitives";
import { cn } from "../lib/utils";

/**
 * UI-4: the trigger's menu renders in a portal with fixed positioning.
 *
 * Inside a table the scroll container clips absolutely-positioned children (the
 * menu looked like it sat "behind" the last row and grew a scrollbar), so the
 * panel is attached to ``document.body`` and anchored to the trigger rect.
 */
export function DropdownMenu({
  children,
  label,
  align = "right",
}: {
  children: ReactNode | ((close: () => void) => ReactNode);
  label?: ReactNode;
  align?: "left" | "right";
}) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const place = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const width = menuRef.current?.offsetWidth ?? 208;
    const height = menuRef.current?.offsetHeight ?? 0;
    const left = align === "right" ? rect.right - width : rect.left;
    let top = rect.bottom + 4;
    if (height && top + height > window.innerHeight - 8) {
      top = Math.max(8, rect.top - height - 4);
    }
    setPosition({
      top,
      left: Math.min(Math.max(8, left), Math.max(8, window.innerWidth - width - 8)),
    });
  }, [align]);

  useLayoutEffect(() => {
    if (open) place();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, place]);

  const close = useCallback(() => setOpen(false), []);

  return (
    <>
      <Button
        ref={triggerRef}
        variant="ghost"
        size="icon"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {label ?? <MoreHorizontal className="h-4 w-4" />}
      </Button>
      {open
        ? createPortal(
            <div
              ref={menuRef}
              role="menu"
              style={{ position: "fixed", top: position?.top ?? -9999, left: position?.left ?? -9999 }}
              className={cn(
                "z-50 min-w-[13rem] overflow-hidden rounded-md border border-border bg-card py-1 shadow-lg",
                !position && "invisible",
              )}
            >
              {typeof children === "function" ? children(close) : children}
            </div>,
            document.body,
          )
        : null}
    </>
  );
}

export function MenuItem({
  children,
  onSelect,
  destructive,
  disabled,
}: {
  children: ReactNode;
  onSelect?: () => void;
  destructive?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      role="menuitem"
      type="button"
      disabled={disabled}
      onClick={onSelect}
      className={cn(
        "block w-full px-3 py-1.5 text-left text-sm transition hover:bg-accent disabled:opacity-50",
        destructive && "text-destructive",
      )}
    >
      {children}
    </button>
  );
}

export function MenuSeparator() {
  return <div className="my-1 border-t border-border" />;
}
