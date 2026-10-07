import { cn } from "../lib/utils";

export interface TabItem {
  id: string;
  label: string;
  badge?: number | string;
}

export function Tabs({
  items,
  value,
  onChange,
  className,
}: {
  items: TabItem[];
  value: string;
  onChange: (id: string) => void;
  className?: string;
}) {
  return (
    <div
      className={cn("inline-flex max-w-full flex-wrap items-center gap-1 rounded-lg border border-border bg-surface p-1", className)}
      role="tablist"
    >
      {items.map((item) => (
        <button
          key={item.id}
          role="tab"
          aria-selected={value === item.id}
          onClick={() => onChange(item.id)}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md px-3.5 py-1.5 text-sm font-medium transition",
            value === item.id
              ? "bg-accent text-foreground shadow-card"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {item.label}
          {item.badge !== undefined && item.badge !== 0 ? (
            <span className="rounded-full bg-primary/15 px-1.5 text-xs font-semibold text-primary">{item.badge}</span>
          ) : null}
        </button>
      ))}
    </div>
  );
}
