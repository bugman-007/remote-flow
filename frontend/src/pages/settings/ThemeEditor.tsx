import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Eye, FileUp, Layers, Palette, RotateCcw, Sparkles, Trash2, Type } from "lucide-react";
import { api, errorMessage } from "../../lib/api";
import { Badge, Button, ErrorNote, Field, Input, Select, Spinner } from "../../ui/primitives";
import { useToast } from "../../ui/toast";
import { cn } from "../../lib/utils";
import { t } from "../../i18n";
import type { Profile, Theme, ThemeEditorSpec, ThemeElementStyle, ThemeParams, ThemeSchema, ThemeTextRule } from "../../types";
import { ThemePreview, type PreviewBlock } from "./ThemePreview";
import { ELEMENT_HELP, ELEMENT_KINDS, ELEMENT_LABELS, type ElementKind } from "./themeStyles";

type Panel = "page" | "type" | "element" | "rules" | "assign";

/** Appendix C.2 fields that only make sense for some kinds. */
const HIDDEN_FIELDS: Partial<Record<ElementKind, string[]>> = {
  section: ["indent_left", "first_line_indent"],
  name: ["indent_left", "first_line_indent"],
};

export function ThemeEditor({
  open,
  theme,
  schema,
  onClose,
  onSaved,
}: {
  open: boolean;
  theme: Theme | null;
  schema: ThemeSchema | undefined;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [params, setParams] = useState<ThemeParams>({});
  const [assigned, setAssigned] = useState<string[]>([]);
  const [selected, setSelected] = useState<ElementKind | null>("name");
  const [panel, setPanel] = useState<Panel>("type");
  const [zoom, setZoom] = useState(0.8);
  const [showHidden, setShowHidden] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [proofUrl, setProofUrl] = useState<string | null>(null);
  const [proofBusy, setProofBusy] = useState(false);
  const [importBusy, setImportBusy] = useState(false);
  const [importSource, setImportSource] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const defaults = schema?.defaults ?? {};
  const editor: ThemeEditorSpec | undefined = schema?.editor;

  const sample = useQuery({
    queryKey: ["theme-sample"],
    queryFn: () => api.get<{ blocks: PreviewBlock[] }>("/themes/sample"),
    enabled: open,
    staleTime: Infinity,
  });

  const profiles = useQuery({
    queryKey: ["profiles"],
    queryFn: () => api.get<{ items: Profile[] }>("/profiles"),
    enabled: open,
  });

  useEffect(() => {
    if (!open) return;
    setName(theme?.name ?? "");
    setDescription(theme?.description ?? "");
    setParams({ ...defaults, ...(theme?.params ?? {}) });
    setAssigned((theme?.profiles ?? []).map((profile) => profile.id));
    setImportSource(null);
    setProofUrl(null);
    setError(null);
  }, [open, theme, defaults]);

  const styles = useMemo(() => params.elements ?? {}, [params.elements]);
  const rules = useMemo(() => params.text_rules ?? [], [params.text_rules]);
  const currentStyle: ThemeElementStyle = selected ? styles[selected] ?? {} : {};

  const setElement = (kind: ElementKind, key: string, value: string | number | boolean | null) => {
    setParams((current) => {
      const elements = { ...(current.elements ?? {}) };
      const next: ThemeElementStyle = { ...(elements[kind] ?? {}) };
      if (value === null) delete next[key];
      else next[key] = value;
      if (Object.keys(next).length === 0) delete elements[kind];
      else elements[kind] = next;
      return { ...current, elements };
    });
  };

  const upsertRule = (patch: Partial<ThemeTextRule> & { text: string }) => {
    setParams((current) => {
      const list = [...(current.text_rules ?? [])];
      const index = list.findIndex((rule) => rule.text.toLowerCase() === patch.text.toLowerCase());
      if (index >= 0) list[index] = { ...list[index], ...patch };
      else list.push(patch as ThemeTextRule);
      return { ...current, text_rules: list };
    });
    setPanel("rules");
  };

  const removeRule = (text: string) => {
    setParams((current) => ({
      ...current,
      text_rules: (current.text_rules ?? []).filter((rule) => rule.text.toLowerCase() !== text.toLowerCase()),
    }));
  };

  const applyPreset = (presetParams: ThemeParams) => {
    setParams(() => ({ ...defaults, ...presetParams, elements: { ...(presetParams.elements ?? {}) } }));
  };

  const renderProof = async () => {
    setError(null);
    setProofBusy(true);
    try {
      const blob = await api.requestBlob("/themes/preview", { method: "POST", body: { params } });
      const url = URL.createObjectURL(blob);
      setProofUrl((current) => {
        if (current) URL.revokeObjectURL(current);
        return url;
      });
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setProofBusy(false);
    }
  };

  const importResume = async (file: File) => {
    setError(null);
    setImportBusy(true);
    try {
      const body = new FormData();
      body.append("file", file);
      const result = await api.post<{
        name: string;
        description: string;
        params: ThemeParams;
        source: { font: string | null; size: number; accent: string };
        warnings: string[];
      }>("/themes/import-resume", body);
      setName(result.name);
      setDescription(result.description);
      setParams({ ...defaults, ...result.params });
      setImportSource(
        t("settings.themes.importSource", {
          font: result.source.font ?? "—",
          size: result.source.size,
          accent: result.source.accent,
        }),
      );
      push({ tone: "success", title: t("settings.themes.importDone", { name: file.name }) });
      if (result.warnings.length) setError(result.warnings.join(" "));
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setImportBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const payload = { name, description, params };
      let id = theme?.id ?? null;
      if (id) await api.patch(`/themes/${id}`, payload);
      else {
        const created = await api.post<Theme>("/themes", payload);
        id = created.id;
      }
      if (id) await api.post(`/themes/${id}/assign`, { profile_ids: assigned });
      push({ tone: "success", title: t("toast.saved") });
      await queryClient.invalidateQueries({ queryKey: ["themes"] });
      onSaved();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  if (!open) return null;

  const globalField = (key: string) => schema?.fields.find((field) => field.key === key);

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-background/95">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-card px-4 py-2">
        <div className="flex flex-1 flex-wrap items-center gap-2">
          <h2 className="text-sm font-semibold">{theme ? t("settings.themes.editTitle") : t("settings.themes.createTitle")}</h2>
          <Input
            className="h-8 w-56"
            placeholder={t("common.name")}
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
          <Input
            className="h-8 w-72"
            placeholder={t("common.description")}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
          {editor?.presets?.length ? (
            <span className="flex items-center gap-1">
              <Sparkles className="h-3.5 w-3.5 text-muted-foreground" />
              {editor.presets.map((preset) => (
                <Button key={preset.key} size="sm" variant="outline" onClick={() => applyPreset(preset.params)}>
                  {preset.name.split("—")[0].trim()}
                </Button>
              ))}
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          {editor ? <Badge tone="outline">{t("settings.themes.engine", { version: editor.generator_version })}</Badge> : null}
          <Button variant="outline" size="sm" onClick={onClose} disabled={busy}>
            {t("common.cancel")}
          </Button>
          <Button size="sm" onClick={() => void save()} loading={busy} disabled={!name}>
            {t("common.save")}
          </Button>
        </div>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[24rem_1fr]">
        <aside className="min-h-0 overflow-y-auto border-r border-border bg-card px-4 py-3">
          <nav className="mb-3 flex flex-wrap gap-1">
            {(
              [
                ["type", t("settings.themes.panelTypography"), Type],
                ["page", t("settings.themes.panelPage"), Palette],
                ["element", t("settings.themes.panelElements"), Layers],
                ["rules", t("settings.themes.panelRules"), Eye],
                ["assign", t("settings.themes.panelAssign"), FileUp],
              ] as const
            ).map(([key, label, Icon]) => (
              <Button
                key={key}
                size="sm"
                variant={panel === key ? "primary" : "outline"}
                onClick={() => setPanel(key)}
              >
                <Icon className="h-3.5 w-3.5" />
                {label}
              </Button>
            ))}
          </nav>

          {panel === "type" ? (
            <div>
              <GlobalFont label={t("settings.themes.fonts")} value={String(params.font ?? "")} options={globalField("font")?.options ?? []} onChange={(value) => setParams({ ...params, font: value })} />
              <NumberControl
                label={t("settings.themes.size")}
                value={Number(params.size ?? 10.5)}
                field={globalField("size")}
                onChange={(value) => setParams({ ...params, size: value })}
              />
              <NumberControl
                label={t("settings.themes.lineHeight")}
                value={Number(params.line_height ?? 1)}
                field={globalField("line_height")}
                onChange={(value) => setParams({ ...params, line_height: value })}
              />
              <ColourControl
                label={t("settings.themes.bodyColour")}
                value={String(params.body_color ?? "#262626")}
                onChange={(value) => setParams({ ...params, body_color: value })}
              />
              <ColourControl
                label={t("settings.themes.mutedColour")}
                value={String(params.muted_color ?? "#595959")}
                onChange={(value) => setParams({ ...params, muted_color: value })}
              />
            </div>
          ) : null}

          {panel === "page" ? (
            <div>
              <Field label={t("settings.themes.pageSize")}>
                <Select
                  value={String(params.page_size ?? "Letter")}
                  onChange={(event) => setParams({ ...params, page_size: event.target.value })}
                >
                  {(editor?.page_sizes ?? ["Letter", "A4"]).map((size) => (
                    <option key={size} value={size}>
                      {size}
                    </option>
                  ))}
                </Select>
              </Field>
              {(
                [
                  ["margin_top", t("settings.themes.marginTop")],
                  ["margin_bottom", t("settings.themes.marginBottom")],
                  ["margin_left", t("settings.themes.marginLeft")],
                  ["margin_right", t("settings.themes.marginRight")],
                ] as const
              ).map(([key, label]) => (
                <NumberControl
                  key={key}
                  label={label}
                  value={Number(params[key] ?? 0.7)}
                  field={globalField(key)}
                  onChange={(value) => setParams({ ...params, [key]: value })}
                />
              ))}
              <NumberControl
                label={t("settings.themes.sectionGap")}
                value={Number(params.section_gap ?? 9)}
                field={globalField("section_gap")}
                onChange={(value) => setParams({ ...params, section_gap: value })}
              />
              <ColourControl
                label={t("settings.themes.accent")}
                value={String(params.accent ?? "#1F4E79")}
                onChange={(value) => setParams({ ...params, accent: value })}
              />
              <ColourControl
                label={t("settings.themes.background")}
                value={String(params.bg_color ?? "#FFFFFF")}
                onChange={(value) => setParams({ ...params, bg_color: value })}
              />
              <Field label={t("settings.themes.bulletGlyph")} hint={t("settings.themes.bulletGlyphHint")}>
                <Input
                  value={String(params.bullet_glyph ?? "•")}
                  maxLength={3}
                  onChange={(event) => setParams({ ...params, bullet_glyph: event.target.value })}
                />
              </Field>
            </div>
          ) : null}

          {panel === "element" ? (
            <div>
              <p className="mb-2 text-xs text-muted-foreground">{t("settings.themes.elementHint")}</p>
              <div className="mb-3 flex flex-wrap gap-1">
                {ELEMENT_KINDS.map((kind) => {
                  const overridden = Object.keys(styles[kind] ?? {}).length > 0;
                  return (
                    <Button
                      key={kind}
                      size="sm"
                      variant={selected === kind ? "primary" : "outline"}
                      onClick={() => setSelected(kind)}
                    >
                      {ELEMENT_LABELS[kind]}
                      {overridden ? <span className="ml-1 h-1.5 w-1.5 rounded-full bg-sky-500" /> : null}
                    </Button>
                  );
                })}
              </div>
              {selected ? (
                <div className="rounded-md border border-border p-3">
                  <div className="mb-2 flex items-center justify-between">
                    <div>
                      <p className="text-sm font-medium">{ELEMENT_LABELS[selected]}</p>
                      <p className="text-xs text-muted-foreground">{ELEMENT_HELP[selected]}</p>
                    </div>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() =>
                        setParams((current) => {
                          const elements = { ...(current.elements ?? {}) };
                          delete elements[selected];
                          return { ...current, elements };
                        })
                      }
                    >
                      <RotateCcw className="h-3.5 w-3.5" />
                      {t("settings.themes.resetElement")}
                    </Button>
                  </div>
                  <ElementControls
                    kind={selected}
                    style={currentStyle}
                    fonts={globalField("font")?.options ?? []}
                    onSet={(key, value) => setElement(selected, key, value)}
                  />
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">{t("settings.themes.pickElement")}</p>
              )}
            </div>
          ) : null}

          {panel === "rules" ? (
            <div>
              <p className="mb-2 text-xs text-muted-foreground">{t("settings.themes.rulesHint")}</p>
              {rules.length === 0 ? (
                <p className="text-sm text-muted-foreground">{t("settings.themes.rulesEmpty")}</p>
              ) : (
                <ul className="space-y-1">
                  {rules.map((rule) => (
                    <li key={rule.text} className="flex items-center justify-between gap-2 rounded border border-border px-2 py-1">
                      <span
                        className="truncate text-xs"
                        style={{ fontWeight: rule.bold ? 700 : 400, backgroundColor: rule.bg ?? undefined, color: rule.color ?? undefined }}
                      >
                        {rule.text}
                      </span>
                      <Button size="icon" variant="ghost" onClick={() => removeRule(rule.text)} title={t("common.delete")}>
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ) : null}

          {panel === "assign" ? (
            <div>
              <Field label={t("settings.themes.assign")}>
                <div className="flex flex-wrap gap-3">
                  {(profiles.data?.items ?? []).map((profile) => (
                    <label key={profile.id} className="flex items-center gap-1 text-sm">
                      <input
                        type="checkbox"
                        className="h-3.5 w-3.5 accent-[hsl(var(--primary))]"
                        checked={assigned.includes(profile.id)}
                        onChange={(event) =>
                          setAssigned(event.target.checked ? [...assigned, profile.id] : assigned.filter((value) => value !== profile.id))
                        }
                      />
                      {profile.name}
                    </label>
                  ))}
                </div>
              </Field>
              <Field label={t("settings.themes.importResume")} hint={t("settings.themes.importHint")}>
                <div className="flex flex-wrap items-center gap-2">
                  <input
                    ref={fileRef}
                    type="file"
                    accept=".docx"
                    className="text-xs"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) void importResume(file);
                    }}
                  />
                  {importBusy ? (
                    <span className="flex items-center gap-2 text-xs text-muted-foreground">
                      <Spinner /> {t("settings.themes.importRunning")}
                    </span>
                  ) : null}
                </div>
                {importSource ? <p className="mt-1 text-xs text-muted-foreground">{importSource}</p> : null}
              </Field>
              <p className="text-xs text-muted-foreground">{t("settings.themes.applies")}</p>
            </div>
          ) : null}

          {error ? (
            <div className="mt-3">
              <ErrorNote>{error}</ErrorNote>
            </div>
          ) : null}
        </aside>

        <section className="flex min-h-0 flex-col">
          <div className="flex items-center justify-between gap-2 border-b border-border bg-card px-4 py-2">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span>{t("settings.themes.livePreview")}</span>
              <label className="flex items-center gap-1">
                <input type="checkbox" className="h-3 w-3" checked={showHidden} onChange={(event) => setShowHidden(event.target.checked)} />
                {t("settings.themes.showHidden")}
              </label>
              <span>
                {Math.round(zoom * 100)}%
                <input
                  type="range"
                  min={0.4}
                  max={1.2}
                  step={0.05}
                  value={zoom}
                  onChange={(event) => setZoom(Number(event.target.value))}
                  className="ml-2 w-24 align-middle"
                />
              </span>
            </div>
            <Button size="sm" variant="outline" onClick={() => void renderProof()} loading={proofBusy}>
              <Eye className="h-3.5 w-3.5" />
              {t("settings.themes.pdfProof")}
            </Button>
          </div>
          <div className="min-h-0 flex-1">
            {sample.isLoading ? (
              <p className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
                <Spinner /> {t("common.loading")}
              </p>
            ) : sample.isError ? (
              <div className="p-4">
                <ErrorNote>{errorMessage(sample.error)}</ErrorNote>
              </div>
            ) : (
              <ThemePreview
                blocks={sample.data?.blocks ?? []}
                params={params}
                selected={selected}
                onSelect={setSelected}
                onRule={upsertRule}
                onRemoveRule={removeRule}
                showHidden={showHidden}
                zoom={zoom}
              />
            )}
          </div>
          {proofUrl ? (
            <div className="h-[38vh] border-t border-border bg-card p-2">
              <div className="mb-1 flex items-center justify-between">
                <span className="text-xs font-medium">{t("settings.themes.pdfProof")}</span>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    URL.revokeObjectURL(proofUrl);
                    setProofUrl(null);
                  }}
                >
                  {t("common.close")}
                </Button>
              </div>
              <iframe title={t("settings.themes.pdfProof")} src={proofUrl} className="h-[32vh] w-full rounded border border-border" />
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}

function GlobalFont({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  return (
    <Field label={label}>
      <Select value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </Select>
    </Field>
  );
}

function NumberControl({
  label,
  value,
  field,
  onChange,
  step,
  min,
  max,
}: {
  label: string;
  value: number;
  field?: { min: number | null; max: number | null; step: number | null };
  onChange: (value: number) => void;
  step?: number;
  min?: number | null;
  max?: number | null;
}) {
  const lo = min ?? field?.min ?? 0;
  const hi = max ?? field?.max ?? 100;
  const increment = step ?? field?.step ?? 1;
  return (
    <Field label={label} hint={`${lo} – ${hi}`}>
      <div className="flex items-center gap-2">
        <input
          type="range"
          min={lo}
          max={hi}
          step={increment}
          value={value}
          onChange={(event) => onChange(Number(event.target.value))}
          className="flex-1"
        />
        <Input
          type="number"
          className="h-8 w-20"
          min={lo}
          max={hi}
          step={increment}
          value={value}
          onChange={(event) => onChange(Number(event.target.value))}
        />
      </div>
    </Field>
  );
}

function ColourControl({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return (
    <Field label={label}>
      <span className="flex items-center gap-2">
        <input
          type="color"
          className="h-9 w-12 rounded border border-input"
          value={value}
          onChange={(event) => onChange(event.target.value)}
        />
        <Input value={value} onChange={(event) => onChange(event.target.value)} />
      </span>
    </Field>
  );
}

/** The per-element style editor; every control writes straight into params.elements. */
function ElementControls({
  kind,
  style,
  fonts,
  onSet,
}: {
  kind: ElementKind;
  style: ThemeElementStyle;
  fonts: string[];
  onSet: (key: string, value: string | number | boolean | null) => void;
}) {
  const hidden = HIDDEN_FIELDS[kind] ?? [];
  const value = (key: string) => style[key];
  const enabled = (key: string) => value(key) !== undefined && value(key) !== null;

  const fields: [string, string][] = [
    ["size", t("settings.themes.fieldSize")],
    ["size_delta", t("settings.themes.fieldSizeDelta")],
    ["weight", t("settings.themes.fieldWeight")],
    ["align", t("settings.themes.fieldAlign")],
    ["color", t("settings.themes.fieldColour")],
    ["bg", t("settings.themes.fieldBackground")],
    ["letter_spacing", t("settings.themes.fieldTracking")],
    ["width_scale", t("settings.themes.fieldWidth")],
    ["line_height", t("settings.themes.fieldLineHeight")],
    ["space_before", t("settings.themes.fieldSpaceBefore")],
    ["space_after", t("settings.themes.fieldSpaceAfter")],
    ["indent_left", t("settings.themes.fieldIndentLeft")],
    ["first_line_indent", t("settings.themes.fieldFirstLine")],
    ["rule_width", t("settings.themes.fieldRuleWidth")],
  ];

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        <ToggleChip label="Bold" active={enabled("weight") ? Number(value("weight")) >= 600 : undefined} onToggle={() => onSet("weight", enabled("weight") && Number(value("weight")) >= 600 ? 400 : 700)} />
        <ToggleChip label="Italic" active={style.italic === true} onToggle={() => onSet("italic", !enabled("italic") ? true : null)} />
        <ToggleChip label="Underline" active={style.underline === true} onToggle={() => onSet("underline", !enabled("underline") ? true : null)} />
        <ToggleChip label="UPPERCASE" active={style.uppercase === true} onToggle={() => onSet("uppercase", !enabled("uppercase") ? true : null)} />
        <ToggleChip label="Hide" active={style.hidden === true} onToggle={() => onSet("hidden", !enabled("hidden") ? true : null)} />
      </div>
      {kind === "section" ? (
        <ToggleChip label={t("settings.themes.fieldRule")} active={!enabled("rule") || style.rule !== false} onToggle={() => onSet("rule", style.rule === false ? null : false)} />
      ) : null}

      <Field label={t("settings.themes.fieldFont")}>
        <Select value={String(value("font") ?? "")} onChange={(event) => onSet("font", event.target.value || null)}>
          <option value="">{t("settings.themes.inherit")}</option>
          {fonts.map((font) => (
            <option key={font} value={font}>
              {font}
            </option>
          ))}
        </Select>
      </Field>

      {fields.map(([key, label]) =>
        hidden.includes(key) ? null : key === "align" ? (
          <Field key={key} label={label}>
            <Select value={String(value(key) ?? "")} onChange={(event) => onSet(key, event.target.value || null)}>
              <option value="">{t("settings.themes.inherit")}</option>
              {["left", "center", "right", "justify"].map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </Select>
          </Field>
        ) : key === "color" || key === "bg" ? (
          <Field key={key} label={label}>
            <span className="flex items-center gap-2">
              <input
                type="color"
                className="h-9 w-12 rounded border border-input"
                value={String(value(key) ?? "#ffffff")}
                onChange={(event) => onSet(key, event.target.value)}
              />
              <Input value={String(value(key) ?? "")} onChange={(event) => onSet(key, event.target.value || null)} />
              {enabled(key) ? (
                <Button size="icon" variant="ghost" title={t("settings.themes.inherit")} onClick={() => onSet(key, null)}>
                  <RotateCcw className="h-3.5 w-3.5" />
                </Button>
              ) : null}
            </span>
          </Field>
        ) : (
          <ElementNumber key={key} label={label} fieldKey={key} value={value(key)} onSet={onSet} />
        ),
      )}
      <ChevronDown className="hidden h-3 w-3" />
    </div>
  );
}

function ElementNumber({
  label,
  fieldKey,
  value,
  onSet,
}: {
  label: string;
  fieldKey: string;
  value: string | number | boolean | null | undefined;
  onSet: (key: string, value: string | number | boolean | null) => void;
}) {
  const bounds = ELEMENT_BOUNDS[fieldKey] ?? [0, 100, 1];
  const numeric = value === undefined || value === null ? bounds[2] : Number(value);
  return (
    <Field label={label} hint={`${bounds[0]} – ${bounds[1]}`}>
      <div className="flex items-center gap-2">
        <input
          type="range"
          min={bounds[0]}
          max={bounds[1]}
          step={bounds[2]}
          value={numeric}
          onChange={(event) => onSet(fieldKey, Number(event.target.value))}
          className="flex-1"
        />
        <Input
          type="number"
          className="h-8 w-20"
          min={bounds[0]}
          max={bounds[1]}
          step={bounds[2]}
          value={value === undefined || value === null ? "" : Number(value)}
          placeholder={String(bounds[2])}
          onChange={(event) => onSet(fieldKey, event.target.value === "" ? null : Number(event.target.value))}
        />
        {value !== undefined && value !== null ? (
          <Button size="icon" variant="ghost" title={t("settings.themes.inherit")} onClick={() => onSet(fieldKey, null)}>
            <RotateCcw className="h-3.5 w-3.5" />
          </Button>
        ) : null}
      </div>
    </Field>
  );
}

function ToggleChip({
  label,
  active,
  onToggle,
}: {
  label: string;
  active: boolean | undefined;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className={cn(
        "rounded-full border px-2.5 py-0.5 text-xs transition",
        active === true ? "border-primary bg-primary text-primary-foreground" : "border-input bg-card hover:bg-accent",
        active === undefined && "text-muted-foreground",
      )}
    >
      {label}
    </button>
  );
}

const ELEMENT_BOUNDS: Record<string, [number, number, number]> = {
  size: [5, 48, 0.5],
  size_delta: [-6, 26, 0.5],
  weight: [100, 900, 100],
  letter_spacing: [-2, 8, 0.1],
  width_scale: [50, 200, 5],
  line_height: [0.8, 3, 0.05],
  space_before: [0, 60, 1],
  space_after: [0, 60, 1],
  indent_left: [0, 3, 0.05],
  indent_right: [0, 3, 0.05],
  first_line_indent: [-1, 1, 0.05],
  rule_width: [0, 24, 1],
};
