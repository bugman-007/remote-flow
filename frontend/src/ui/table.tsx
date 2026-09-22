import { Children, cloneElement, isValidElement, type ReactNode } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button, Spinner } from "./primitives";
import { t } from "../i18n";
import type { Pagination } from "../types";

/** UI-3: read the visible header labels so cells can be labelled when the table stacks. */
function labelText(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(labelText).join(" ").trim();
  if (isValidElement(node)) return labelText((node.props as { children?: ReactNode }).children);
  return "";
}

/** Sort arrows are decoration; drop them from the mobile label. */
function cleanLabel(text: string): string {
  return text.replace(/[↑↓]/g, "").replace(/\s+/g, " ").trim();
}

function headerLabels(children: ReactNode): string[] {
  for (const child of Children.toArray(children)) {
    if (!isValidElement(child) || child.type !== "thead") continue;
    for (const row of Children.toArray((child.props as { children?: ReactNode }).children)) {
      if (!isValidElement(row) || row.type !== "tr") continue;
      return Children.toArray((row.props as { children?: ReactNode }).children)
        .filter((cell) => isValidElement(cell))
        .map((cell) => cleanLabel(labelText((cell.props as { children?: ReactNode }).children)));
    }
  }
  return [];
}

function injectLabels(child: ReactNode, labels: string[]): ReactNode {
  if (!isValidElement(child) || child.type !== "tbody" || !labels.length) return child;
  const rows = Children.map((child.props as { children?: ReactNode }).children, (row) => {
    if (!isValidElement(row) || row.type !== "tr") return row;
    let index = 0;
    const cells = Children.map((row.props as { children?: ReactNode }).children, (cell) => {
      if (!isValidElement(cell) || cell.type !== "td") return cell;
      const label = labels[index++] ?? "";
      if (!label || (cell.props as { "data-label"?: string })["data-label"]) return cell;
      return cloneElement(cell, { "data-label": label } as Record<string, unknown>);
    });
    return cloneElement(row, undefined, cells);
  });
  return cloneElement(child, undefined, rows);
}

export function Table({ children, className }: { children: ReactNode; className?: string }) {
  const labels = headerLabels(children);
  return (
    <div className={"rf-scroll " + (className ?? "")}>
      <table className="rf-table">
        {labels.length ? Children.map(children, (child) => injectLabels(child, labels)) : children}
      </table>
    </div>
  );
}

export function TableState({ loading, error, empty, colSpan }: { loading: boolean; error?: ReactNode; empty: ReactNode; colSpan: number }) {
  if (loading) {
    return (
      <tr>
        <td colSpan={colSpan} className="py-8 text-center text-muted-foreground">
          <Spinner /> <span className="ml-2 text-sm">{t("common.loading")}</span>
        </td>
      </tr>
    );
  }
  if (error) {
    return (
      <tr>
        <td colSpan={colSpan} className="py-8 text-center text-sm text-destructive">
          {error}
        </td>
      </tr>
    );
  }
  return (
    <tr>
      <td colSpan={colSpan} className="py-8 text-center text-sm text-muted-foreground">
        {empty}
      </td>
    </tr>
  );
}

export function Pager({ pagination, onChange }: { pagination?: Pagination; onChange: (page: number) => void }) {
  if (!pagination || pagination.pages <= 1) {
    return pagination ? <p className="text-xs text-muted-foreground">{t("common.total", { count: pagination.total })}</p> : null;
  }
  return (
    <div className="mt-3 flex items-center justify-between gap-3 text-xs text-muted-foreground">
      <span>{t("common.total", { count: pagination.total })}</span>
      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={pagination.page <= 1}
          onClick={() => onChange(pagination.page - 1)}
        >
          <ChevronLeft className="h-3.5 w-3.5" />
          {t("common.previous")}
        </Button>
        <span>
          {t("common.page")} {pagination.page} {t("common.of")} {pagination.pages}
        </span>
        <Button
          variant="outline"
          size="sm"
          disabled={pagination.page >= pagination.pages}
          onClick={() => onChange(pagination.page + 1)}
        >
          {t("common.next")}
          <ChevronRight className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  );
}
