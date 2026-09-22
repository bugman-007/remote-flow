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
    <div className={cn("flex flex-wrap items-center gap-1 border-b border-border", className)} role="tablist">
      {items.map((item) => (
        <button
          key={item.id}
          role="tab"
          aria-selected={value === item.id}
          onClick={() => onChange(item.id)}
          className={cn(
            "-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium transition",
            value === item.id
              ? "border-primary text-foreground"
              : "border-transparent text-muted-foreground hover:text-foreground",
          )}
        >
          {item.label}
          {item.badge !== undefined && item.badge !== 0 ? (
            <span className="rounded-full bg-muted px-1.5 text-xs text-muted-foreground">{item.badge}</span>
          ) : null}
        </button>
      ))}
    </div>
  );
}
