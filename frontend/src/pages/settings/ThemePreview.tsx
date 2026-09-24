import { useRef, useState } from "react";
import { AlignCenter, Bold, Highlighter, Italic, Underline, X } from "lucide-react";
import { Button } from "../../ui/primitives";
import { cn } from "../../lib/utils";
import type { ThemeParams, ThemeTextRule } from "../../types";
import {
  applyTextRules,
  ELEMENT_LABELS,
  elementCss,
  PAGE_SIZES,
  resolveElement,
  runCss,
  type ElementKind,
  type PreviewRun,
} from "./themeStyles";

export interface PreviewBlock {
  kind: ElementKind;
  label?: string | null;
  align_right?: string | null;
  runs: PreviewRun[];
  text: string;
}

const HIGHLIGHT = "#FEF08A";

/**
 * SET-10: the full document, styled with the unsaved theme, rendered in the
 * browser. Click a region to select that element; select text inside a region
 * to add a text rule that follows the phrase into every generated resume.
 */
export function ThemePreview({
  blocks,
  params,
  selected,
  onSelect,
  onRule,
  onRemoveRule,
  showHidden,
  zoom,
}: {
  blocks: PreviewBlock[];
  params: ThemeParams;
  selected: ElementKind | null;
  onSelect: (kind: ElementKind | null) => void;
  onRule: (patch: Partial<ThemeTextRule> & { text: string }) => void;
  onRemoveRule: (text: string) => void;
  showHidden: boolean;
  zoom: number;
}) {
  const pageRef = useRef<HTMLDivElement | null>(null);
  const [pick, setPick] = useState<{ text: string; top: number; left: number; existing: boolean } | null>(null);

  const page = PAGE_SIZES[String(params.page_size ?? "Letter")] ?? PAGE_SIZES.Letter;
  const margin = {
    top: Number(params.margin_top ?? 0.7),
    bottom: Number(params.margin_bottom ?? 0.7),
    left: Number(params.margin_left ?? 0.75),
    right: Number(params.margin_right ?? 0.75),
  };
  const bodyBackground = String(params.bg_color ?? "#FFFFFF");

  const rules = params.text_rules ?? [];

  const captureSelection = () => {
    const selection = window.getSelection();
    const container = pageRef.current;
    if (!selection || selection.isCollapsed || !container) {
      setPick(null);
      return;
    }
    const text = selection.toString().replace(/\s+/g, " ").trim();
    const anchorNode = selection.anchorNode;
    if (!text || text.length > 160 || !anchorNode || !container.contains(anchorNode)) {
      setPick(null);
      return;
    }
    const rect = selection.getRangeAt(0).getBoundingClientRect();
    const existing = rules.some((rule) => rule.text.toLowerCase() === text.toLowerCase());
    setPick({ text, top: rect.top - 44, left: Math.max(8, rect.left), existing });
  };

  return (
    <div className="relative h-full overflow-auto rounded-lg border border-border bg-muted/40 p-6">
      {pick ? (
        <div
          className="fixed z-50 flex items-center gap-1 rounded-md border border-border bg-card px-1.5 py-1 shadow-lg"
          style={{ top: Math.max(8, pick.top), left: pick.left }}
        >
          <span className="max-w-[10rem] truncate px-1 text-xs text-muted-foreground" title={pick.text}>
            “{pick.text}”
          </span>
          <Button size="icon" variant="ghost" title="Bold" onClick={() => onRule({ text: pick.text, bold: true })}>
            <Bold className="h-3.5 w-3.5" />
          </Button>
          <Button size="icon" variant="ghost" title="Italic" onClick={() => onRule({ text: pick.text, italic: true })}>
            <Italic className="h-3.5 w-3.5" />
          </Button>
          <Button size="icon" variant="ghost" title="Underline" onClick={() => onRule({ text: pick.text, underline: true })}>
            <Underline className="h-3.5 w-3.5" />
          </Button>
          <Button
            size="icon"
            variant="ghost"
            title="Highlight"
            onClick={() => onRule({ text: pick.text, bg: HIGHLIGHT, bold: true })}
          >
            <Highlighter className="h-3.5 w-3.5" />
          </Button>
          <Button
            size="icon"
            variant="ghost"
            title="Not bold / clear"
            onClick={() => onRule({ text: pick.text, bold: false, italic: false, underline: false })}
          >
            <AlignCenter className="h-3.5 w-3.5" />
          </Button>
          <Button
            size="icon"
            variant="ghost"
            title="Remove rule"
            disabled={!pick.existing}
            onClick={() => onRemoveRule(pick.text)}
          >
            <X className="h-3.5 w-3.5" />
          </Button>
        </div>
      ) : null}

      <div
        className="mx-auto origin-top"
        style={{ width: `${page.width}in`, transform: `scale(${zoom})`, transformOrigin: "top center" }}
      >
        <div
          ref={pageRef}
          onMouseUp={captureSelection}
          className="shadow-md"
          style={{
            width: `${page.width}in`,
            minHeight: `${page.height}in`,
            background: bodyBackground === "#FFFFFF" ? "#FFFFFF" : bodyBackground,
            paddingTop: `${margin.top}in`,
            paddingBottom: `${margin.bottom}in`,
            paddingLeft: `${margin.left}in`,
            paddingRight: `${margin.right}in`,
            boxSizing: "border-box",
          }}
        >
          {blocks.map((block, index) => {
            const style = resolveElement(params, block.kind);
            if (style.hidden && !showHidden) return null;
            const isSelected = selected === block.kind;
            const runs = applyTextRules(block.runs, rules);
            return (
              <div
                key={`${block.kind}-${index}`}
                data-kind={block.kind}
                title={ELEMENT_LABELS[block.kind]}
                onClick={(event) => {
                  if (window.getSelection()?.toString()) return;
                  event.stopPropagation();
                  onSelect(isSelected ? null : block.kind);
                }}
                className={cn(
                  "cursor-pointer rounded-[2px] transition",
                  isSelected ? "outline outline-2 outline-offset-1 outline-sky-500" : "hover:outline hover:outline-1 hover:outline-sky-300",
                  style.hidden && "opacity-30 outline-dashed outline-1 outline-amber-400",
                )}
                style={elementCss(style)}
              >
                {block.kind === "bullet" ? (
                  <span style={{ color: style.colorSet ? style.color : style.accent }}>
                    {String(params.bullet_glyph ?? "•")}
                    {"  "}
                  </span>
                ) : null}
                {block.kind === "skills" && block.label ? (
                  <span style={{ fontWeight: 700 }}>
                    {block.label}
                    {": "}
                  </span>
                ) : null}
                {runs.map((run, runIndex) => (
                  <span key={runIndex} style={runCss(run, style)}>
                    {run.text}
                  </span>
                ))}
                {block.align_right ? (
                  <span style={{ float: "right", color: style.muted, fontWeight: 400, fontSize: "0.9em" }}>
                    {block.align_right}
                  </span>
                ) : null}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
