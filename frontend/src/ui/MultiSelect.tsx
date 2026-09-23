import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, X } from "lucide-react";
import { Button, Input } from "./primitives";
import { cn } from "../lib/utils";
import { t } from "../i18n";

export interface MultiSelectOption {
  value: string;
  label: string;
  hint?: string;
}

/**
 * A searchable checkbox dropdown (the Notion-style multi-select used by the
 * Resumes/Interviews filter bars). Values stay an array; an empty array means
 * "no filter".
 */
export function MultiSelect({
  options,
  values,
  onChange,
  placeholder,
  disabled = false,
  className,
  align = "left",
}: {
  options: MultiSelectOption[];
  values: string[];
  onChange: (values: string[]) => void;
  placeholder: string;
  disabled?: boolean;
  className?: string;
  align?: "left" | "right";
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const filtered = useMemo(() => {
    const term = query.trim().toLowerCase();
    if (!term) return options;
    return options.filter((option) => option.label.toLowerCase().includes(term));
  }, [options, query]);

  const toggle = (value: string) => {
    onChange(values.includes(value) ? values.filter((item) => item !== value) : [...values, value]);
  };

  const label =
    values.length === 0
      ? placeholder
      : values.length === 1
        ? options.find((option) => option.value === values[0])?.label ?? placeholder
        : t("common.nSelected", { count: values.length });

  return (
    <div className={cn("relative inline-block text-left", className)} ref={ref}>
      <Button
        variant="outline"
        size="sm"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className={cn("max-w-[14rem] justify-between gap-2 font-normal", values.length && "border-primary/60")}
      >
        <span className="truncate">{label}</span>
        {values.length ? (
          <span
            role="button"
            tabIndex={0}
            aria-label={t("common.clear")}
            className="rounded p-0.5 hover:bg-accent"
            onClick={(event) => {
              event.stopPropagation();
              onChange([]);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                event.stopPropagation();
                onChange([]);
              }
            }}
          >
            <X className="h-3 w-3" />
          </span>
        ) : (
          <ChevronDown className="h-3 w-3 opacity-60" />
        )}
      </Button>
      {open ? (
        <div
          role="listbox"
          className={cn(
            "absolute z-40 mt-1 w-64 rounded-md border border-border bg-card p-2 shadow-lg",
            align === "right" ? "right-0" : "left-0",
          )}
        >
          <Input
            autoFocus
            className="mb-2 h-8 text-xs"
            placeholder={t("common.search")}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <div className="max-h-60 overflow-y-auto">
            {filtered.length === 0 ? (
              <p className="px-1 py-2 text-xs text-muted-foreground">{t("common.noResults")}</p>
            ) : (
              filtered.map((option) => {
                const checked = values.includes(option.value);
                return (
                  <button
                    key={option.value}
                    type="button"
                    role="option"
                    aria-selected={checked}
                    onClick={() => toggle(option.value)}
                    className="flex w-full items-center gap-2 rounded px-1.5 py-1.5 text-left text-xs hover:bg-accent"
                  >
                    <span
                      className={cn(
                        "flex h-3.5 w-3.5 flex-none items-center justify-center rounded border border-input",
                        checked && "border-primary bg-primary text-primary-foreground",
                      )}
                    >
                      {checked ? <Check className="h-2.5 w-2.5" /> : null}
                    </span>
                    <span className="flex-1 truncate">{option.label}</span>
                    {option.hint ? <span className="text-muted-foreground">{option.hint}</span> : null}
                  </button>
                );
              })
            )}
          </div>
          {values.length ? (
            <button
              type="button"
              className="mt-1 w-full rounded px-1.5 py-1 text-left text-xs text-muted-foreground hover:bg-accent"
              onClick={() => onChange([])}
            >
              {t("common.clear")}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
