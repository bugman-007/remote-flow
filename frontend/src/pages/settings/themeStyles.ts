import type { CSSProperties } from "react";
import type { ThemeParams, ThemeTextRule } from "../../types";

/**
 * Browser mirror of `vendor/resume_builder/core.py` (Appendix C.2).
 *
 * The DOCX writer and this resolver must agree: both start from the same
 * per-kind base style, add the theme's global values, then apply the theme's
 * element overrides. Keep the tables below in sync with core.ELEMENT_BASE,
 * core.KIND_PROPS and core.DEFAULTS.
 */

export const ELEMENT_KINDS = [
  "name",
  "title",
  "contact",
  "section",
  "job",
  "meta",
  "bullet",
  "skills",
  "body",
] as const;

export type ElementKind = (typeof ELEMENT_KINDS)[number];

export const ELEMENT_LABELS: Record<ElementKind, string> = {
  name: "Name",
  title: "Headline",
  contact: "Contact line",
  section: "Section heading",
  job: "Job / entry line",
  meta: "Dates & meta",
  bullet: "Bullets",
  skills: "Skill rows",
  body: "Paragraphs",
};

export const ELEMENT_HELP: Record<ElementKind, string> = {
  name: "The candidate's name at the top of page 1.",
  title: "The headline under the name.",
  contact: "Email, phone, location, work authorisation and LinkedIn.",
  section: "Summary, Technical Skills, Experience, Education headings.",
  job: "Company — role lines and degree — school lines.",
  meta: "Location, GPA and the right-aligned date range.",
  bullet: "Experience bullet points.",
  skills: "Category: value rows in Technical Skills.",
  body: "Free paragraphs, including the summary.",
};

export const ELEMENT_FIELD_KEYS = [
  "font",
  "size",
  "size_delta",
  "weight",
  "italic",
  "underline",
  "uppercase",
  "align",
  "color",
  "bg",
  "letter_spacing",
  "width_scale",
  "line_height",
  "space_before",
  "space_after",
  "indent_left",
  "indent_right",
  "first_line_indent",
  "rule",
  "rule_width",
  "hidden",
] as const;

interface ElementBase {
  sizeDelta: number;
  tone: "accent" | "body" | "muted";
  bold: boolean;
  align?: string;
  rule?: boolean;
  ruleWidth?: number;
  glyph?: boolean;
  indentLeft?: number;
  firstLineIndent?: number;
}

export const ELEMENT_BASE: Record<ElementKind, ElementBase> = {
  name: { sizeDelta: 11, tone: "accent", bold: true },
  title: { sizeDelta: 2.5, tone: "accent", bold: true },
  contact: { sizeDelta: -0.5, tone: "muted", bold: false },
  section: { sizeDelta: 1, tone: "accent", bold: true, rule: true, ruleWidth: 6 },
  job: { sizeDelta: 0.5, tone: "body", bold: true },
  meta: { sizeDelta: -0.5, tone: "muted", bold: false },
  bullet: {
    sizeDelta: 0,
    tone: "body",
    bold: false,
    glyph: true,
    indentLeft: 0.18,
    firstLineIndent: -0.18,
  },
  skills: { sizeDelta: 0, tone: "body", bold: false },
  body: { sizeDelta: 0, tone: "body", bold: false, align: "justify" },
};

/** Space before/after in points, per kind (core.KIND_PROPS). */
export const KIND_SPACING: Record<ElementKind, [number, number]> = {
  name: [0, 1],
  title: [0, 2],
  contact: [0, 1],
  section: [0, 1.5],
  job: [3, 0],
  meta: [0, 1],
  bullet: [0, 1],
  body: [1, 2],
  skills: [0, 1],
};

export const ALIGN_CHOICES = ["left", "center", "right", "justify"] as const;

export const FALLBACK: Required<Pick<ThemeParams, "font" | "size" | "accent" | "bg_color" | "line_height" | "section_gap" | "margin_top" | "margin_bottom" | "margin_left" | "margin_right" | "body_color" | "muted_color" | "bullet_glyph" | "page_size">> = {
  font: "Calibri",
  size: 10.5,
  accent: "#1F4E79",
  bg_color: "#FFFFFF",
  line_height: 1.0,
  section_gap: 9,
  margin_top: 0.7,
  margin_bottom: 0.7,
  margin_left: 0.75,
  margin_right: 0.75,
  body_color: "#262626",
  muted_color: "#595959",
  bullet_glyph: "•",
  page_size: "Letter",
};

export interface ResolvedElement {
  kind: ElementKind;
  font: string;
  size: number;
  weight: number;
  bold: boolean;
  italic: boolean;
  underline: boolean;
  uppercase: boolean;
  align: string | null;
  color: string;
  colorSet: boolean;
  bg: string | null;
  letterSpacing: number;
  widthScale: number;
  lineHeight: number;
  spaceBefore: number;
  spaceAfter: number;
  indentLeft: number | null;
  indentRight: number | null;
  firstLineIndent: number | null;
  rule: boolean;
  ruleWidth: number;
  hidden: boolean;
  glyph: boolean;
  accent: string;
  muted: string;
}

function normaliseHex(value: unknown, fallback: string): string {
  const raw = String(value ?? "").trim().replace(/^#/, "");
  if (/^[0-9a-fA-F]{6}$/.test(raw)) return `#${raw.toUpperCase()}`;
  if (/^[0-9a-fA-F]{3}$/.test(raw)) {
    return `#${raw
      .split("")
      .map((char) => char + char)
      .join("")
      .toUpperCase()}`;
  }
  return fallback.startsWith("#") ? fallback.toUpperCase() : `#${fallback.toUpperCase()}`;
}

function num(value: unknown, fallback: number): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/** core.resolve_element() — the single source of truth for a kind's look. */
export function resolveElement(params: ThemeParams, kind: ElementKind): ResolvedElement {
  const baseSize = num(params.size, FALLBACK.size);
  const palette = {
    accent: normaliseHex(params.accent ?? FALLBACK.accent, FALLBACK.accent),
    body: normaliseHex(params.body_color ?? FALLBACK.body_color, FALLBACK.body_color),
    muted: normaliseHex(params.muted_color ?? FALLBACK.muted_color, FALLBACK.muted_color),
  };
  const base = ELEMENT_BASE[kind];
  const override = (params.elements?.[kind] ?? {}) as Record<string, unknown>;
  const [spaceBefore, spaceAfter] = KIND_SPACING[kind];
  const sectionGap = num(params.section_gap, FALLBACK.section_gap);

  let weight = base.bold ? 700 : 400;
  if (override.bold !== undefined && override.bold !== null) weight = override.bold ? 700 : 400;
  if (override.weight !== undefined && override.weight !== null) weight = num(override.weight, weight);

  const resolved: ResolvedElement = {
    kind,
    font: String(params.font ?? FALLBACK.font),
    size: baseSize + base.sizeDelta,
    weight,
    bold: weight >= 600,
    italic: false,
    underline: false,
    uppercase: false,
    align: base.align ?? null,
    color: palette[base.tone],
    colorSet: false,
    bg: null,
    letterSpacing: 0,
    widthScale: 100,
    lineHeight: num(params.line_height, FALLBACK.line_height),
    spaceBefore: spaceBefore + (kind === "section" ? sectionGap : 0),
    spaceAfter,
    indentLeft: base.indentLeft ?? null,
    indentRight: null,
    firstLineIndent: base.firstLineIndent ?? null,
    rule: Boolean(base.rule),
    ruleWidth: base.ruleWidth ?? 6,
    hidden: false,
    glyph: Boolean(base.glyph),
    accent: palette.accent,
    muted: palette.muted,
  };

  for (const [key, raw] of Object.entries(override)) {
    if (raw === null || raw === undefined) continue;
    switch (key) {
      case "bold":
      case "weight":
        break;
      case "size":
        resolved.size = num(raw, resolved.size);
        break;
      case "size_delta":
        resolved.size = baseSize + base.sizeDelta + num(raw, 0);
        break;
      case "color":
        resolved.color = normaliseHex(raw, palette.body);
        resolved.colorSet = true;
        break;
      case "bg":
        resolved.bg = normaliseHex(raw, "#FFFFFF");
        break;
      case "align":
        if (ALIGN_CHOICES.includes(String(raw) as (typeof ALIGN_CHOICES)[number])) resolved.align = String(raw);
        break;
      case "italic":
        resolved.italic = Boolean(raw);
        break;
      case "underline":
        resolved.underline = Boolean(raw);
        break;
      case "uppercase":
        resolved.uppercase = Boolean(raw);
        break;
      case "rule":
        resolved.rule = Boolean(raw);
        break;
      case "hidden":
        resolved.hidden = Boolean(raw);
        break;
      default:
        if (key in resolved) (resolved as unknown as Record<string, number>)[key] = num(raw, 0);
    }
  }
  return resolved;
}

/** The element's CSS, in the same units the DOCX uses (pt for type, in for the page). */
export function elementCss(style: ResolvedElement): CSSProperties {
  const css: CSSProperties = {
    fontFamily: `${style.font}, Carlito, Caladea, Liberation Sans, sans-serif`,
    fontSize: `${style.size}pt`,
    fontWeight: style.weight,
    fontStyle: style.italic ? "italic" : "normal",
    textDecoration: style.underline ? "underline" : "none",
    textTransform: style.uppercase ? "uppercase" : "none",
    color: style.color,
    lineHeight: style.lineHeight,
    letterSpacing: style.letterSpacing ? `${style.letterSpacing}pt` : undefined,
    marginTop: `${style.spaceBefore}pt`,
    marginBottom: `${style.spaceAfter}pt`,
    textAlign: (style.align ?? "left") as CSSProperties["textAlign"],
  };
  if (style.widthScale !== 100) css.fontStretch = `${style.widthScale}%`;
  if (style.bg) css.backgroundColor = style.bg;
  if (style.indentLeft !== null) css.paddingLeft = `${style.indentLeft}in`;
  if (style.indentRight !== null) css.paddingRight = `${style.indentRight}in`;
  if (style.firstLineIndent !== null) css.textIndent = `${style.firstLineIndent}in`;
  if (style.rule && style.ruleWidth > 0) {
    css.borderBottom = `${Math.max(0.5, style.ruleWidth / 8)}pt solid ${style.color}`;
    css.paddingBottom = "1pt";
  }
  return css;
}

export interface PreviewRun {
  text: string;
  bold: boolean;
  color?: string | null;
  italic?: boolean | null;
  underline?: boolean | null;
  uppercase?: boolean | null;
  bg?: string | null;
}

/** core.apply_text_rules() — split runs on every occurrence of a rule's text. */
export function applyTextRules(runs: PreviewRun[], rules: ThemeTextRule[] | undefined): PreviewRun[] {
  const active = (rules ?? []).filter((rule) => rule && String(rule.text ?? "").trim());
  if (active.length === 0) return runs;
  const out: PreviewRun[] = [];
  for (const run of runs) {
    const text = run.text ?? "";
    const lowered = text.toLowerCase();
    const spans: { start: number; end: number; rule: ThemeTextRule }[] = [];
    for (const rule of active) {
      const needle = String(rule.text).trim().toLowerCase();
      if (!needle) continue;
      let from = 0;
      for (;;) {
        const index = lowered.indexOf(needle, from);
        if (index < 0) break;
        spans.push({ start: index, end: index + needle.length, rule });
        from = index + needle.length;
      }
    }
    if (spans.length === 0) {
      out.push(run);
      continue;
    }
    const cuts = Array.from(new Set([0, text.length, ...spans.flatMap((span) => [span.start, span.end])])).sort(
      (a, b) => a - b,
    );
    for (let i = 0; i < cuts.length - 1; i += 1) {
      const left = cuts[i];
      const right = cuts[i + 1];
      if (left >= right) continue;
      const covering = spans.filter((span) => span.start <= left && right <= span.end).map((span) => span.rule);
      const fragment: PreviewRun = { ...run, text: text.slice(left, right) };
      for (const rule of covering) {
        if (rule.weight !== undefined && rule.weight !== null) fragment.bold = Number(rule.weight) >= 600;
        if (rule.bold !== undefined && rule.bold !== null) fragment.bold = Boolean(rule.bold);
        if (rule.italic !== undefined && rule.italic !== null) fragment.italic = Boolean(rule.italic);
        if (rule.underline !== undefined && rule.underline !== null) fragment.underline = Boolean(rule.underline);
        if (rule.uppercase !== undefined && rule.uppercase !== null) fragment.uppercase = Boolean(rule.uppercase);
        if (rule.color) fragment.color = rule.color;
        if (rule.bg) fragment.bg = rule.bg;
      }
      out.push(fragment);
    }
  }
  return out;
}

export function runCss(run: PreviewRun, style: ResolvedElement): CSSProperties {
  const css: CSSProperties = {};
  if (run.bold) css.fontWeight = 700;
  if (run.color) css.color = run.color;
  if (run.italic) css.fontStyle = "italic";
  if (run.underline) css.textDecoration = "underline";
  if (run.uppercase) css.textTransform = "uppercase";
  if (run.bg) css.backgroundColor = run.bg;
  if (style.letterSpacing) css.letterSpacing = `${style.letterSpacing}pt`;
  if (style.widthScale !== 100) css.fontStretch = `${style.widthScale}%`;
  return css;
}

export const PAGE_SIZES: Record<string, { width: number; height: number }> = {
  Letter: { width: 8.5, height: 11 },
  A4: { width: 8.27, height: 11.69 },
};
