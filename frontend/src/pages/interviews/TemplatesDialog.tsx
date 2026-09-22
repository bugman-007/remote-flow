import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
import { api, errorMessage } from "../../lib/api";
import { Dialog } from "../../ui/dialog";
import { Badge, Button, Checkbox, ErrorNote, Field, Input, Select } from "../../ui/primitives";
import { slugify } from "../../lib/utils";
import { t } from "../../i18n";
import type { InterviewTemplate, InterviewTemplateField } from "../../types";

/** INT-2: form templates with ordered custom fields plus the built-in fields. */
export function TemplatesDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ name: string; is_default: boolean; fields: InterviewTemplateField[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const templates = useQuery({
    queryKey: ["interview-templates"],
    queryFn: () => api.get<{ items: InterviewTemplate[]; builtin_fields: InterviewTemplateField[]; field_types: string[] }>("/interview-templates"),
    enabled: open,
  });

  useEffect(() => {
    if (!open || draft) return;
    const first = templates.data?.items?.[0];
    if (first) {
      setSelectedId(first.id);
      setDraft({ name: first.name, is_default: first.is_default, fields: first.fields });
    }
  }, [open, templates.data, draft]);

  const builtin = templates.data?.builtin_fields ?? [];
  const fieldTypes = templates.data?.field_types ?? [];

  const save = async () => {
    if (!draft) return;
    setBusy(true);
    setError(null);
    try {
      const payload = { name: draft.name, is_default: draft.is_default, fields: draft.fields };
      if (selectedId) await api.put(`/interview-templates/${selectedId}`, payload);
      else {
        const created = await api.post<InterviewTemplate>("/interview-templates", payload);
        setSelectedId(created.id);
      }
      await queryClient.invalidateQueries({ queryKey: ["interview-templates"] });
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!selectedId) return;
    if (!window.confirm(t("confirm.deleteTemplate", { name: draft?.name ?? "" }))) return;
    try {
      await api.del(`/interview-templates/${selectedId}`);
      setSelectedId(null);
      setDraft(null);
      await queryClient.invalidateQueries({ queryKey: ["interview-templates"] });
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };

  const move = (index: number, delta: number) => {
    if (!draft) return;
    const fields = [...draft.fields];
    const target = index + delta;
    if (target < 0 || target >= fields.length) return;
    [fields[index], fields[target]] = [fields[target], fields[index]];
    setDraft({ ...draft, fields });
  };

  return (
    <Dialog open={open} onClose={onClose} title={t("interviews.templateEditor")} width="max-w-4xl">
      <div className="flex flex-wrap items-center gap-2">
        {(templates.data?.items ?? []).map((template) => (
          <Button
            key={template.id}
            size="sm"
            variant={template.id === selectedId ? "primary" : "outline"}
            onClick={() => {
              setSelectedId(template.id);
              setDraft({ name: template.name, is_default: template.is_default, fields: template.fields });
            }}
          >
            {template.name}
            {template.is_default ? " ★" : ""}
          </Button>
        ))}
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            setSelectedId(null);
            setDraft({ name: t("interviews.createTemplate"), is_default: false, fields: [] });
          }}
        >
          <Plus className="h-3.5 w-3.5" />
          {t("interviews.createTemplate")}
        </Button>
      </div>

      {draft ? (
        <div className="mt-4 space-y-3">
          <div className="grid gap-3 tablet:grid-cols-2">
            <Field label={t("interviews.templateName")}>
              <Input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
            </Field>
            <Field label={t("interviews.templateDefault")}>
              <Checkbox
                checked={draft.is_default}
                label={t("interviews.templateDefault")}
                onChange={(event) => setDraft({ ...draft, is_default: event.target.checked })}
              />
            </Field>
          </div>

          <div>
            <p className="rf-label">{t("interviews.fieldLabel")}</p>
            <ul className="space-y-1">
              {builtin.map((field) => (
                <li key={field.key} className="flex items-center gap-2 rounded border border-dashed border-border px-2 py-1 text-xs text-muted-foreground">
                  <Badge tone="outline">{field.type}</Badge>
                  {field.label}
                  <span className="ml-auto">{t("interviews.builtinLocked")}</span>
                </li>
              ))}
              {draft.fields.map((field, index) => (
                <li key={`${field.key}-${index}`} className="rounded border border-border p-2">
                  <div className="flex flex-wrap items-end gap-2">
                    <Field className="mb-0 w-40" label={t("interviews.fieldLabel")}>
                      <Input
                        value={field.label}
                        onChange={(event) => {
                          const fields = [...draft.fields];
                          const label = event.target.value;
                          fields[index] = { ...field, label, key: field.builtin ? field.key : slugify(label) };
                          setDraft({ ...draft, fields });
                        }}
                      />
                    </Field>
                    <Field className="mb-0 w-36" label={t("interviews.fieldType")}>
                      <Select
                        value={field.type}
                        onChange={(event) => {
                          const fields = [...draft.fields];
                          fields[index] = { ...field, type: event.target.value };
                          setDraft({ ...draft, fields });
                        }}
                      >
                        {fieldTypes.map((type) => (
                          <option key={type} value={type}>
                            {type}
                          </option>
                        ))}
                      </Select>
                    </Field>
                    <Field className="mb-0 w-28" label={t("interviews.fieldKey")}>
                      <Input value={field.key} readOnly className="bg-muted" />
                    </Field>
                    <Field className="mb-0 flex-1" label={t("interviews.fieldHelp")}>
                      <Input
                        value={field.help ?? ""}
                        onChange={(event) => {
                          const fields = [...draft.fields];
                          fields[index] = { ...field, help: event.target.value };
                          setDraft({ ...draft, fields });
                        }}
                      />
                    </Field>
                    <Checkbox
                      label={t("interviews.fieldRequired")}
                      checked={field.required}
                      onChange={(event) => {
                        const fields = [...draft.fields];
                        fields[index] = { ...field, required: event.target.checked };
                        setDraft({ ...draft, fields });
                      }}
                    />
                    <Checkbox
                      label={t("interviews.fieldVisible")}
                      checked={field.visible_to_reviewer}
                      onChange={(event) => {
                        const fields = [...draft.fields];
                        fields[index] = { ...field, visible_to_reviewer: event.target.checked };
                        setDraft({ ...draft, fields });
                      }}
                    />
                    <span className="flex items-center gap-1">
                      <Button size="icon" variant="ghost" aria-label={t("interviews.moveUp")} onClick={() => move(index, -1)}>
                        <ArrowUp className="h-3.5 w-3.5" />
                      </Button>
                      <Button size="icon" variant="ghost" aria-label={t("interviews.moveDown")} onClick={() => move(index, 1)}>
                        <ArrowDown className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        aria-label={t("common.delete")}
                        onClick={() => setDraft({ ...draft, fields: draft.fields.filter((_, position) => position !== index) })}
                      >
                        <Trash2 className="h-3.5 w-3.5 text-destructive" />
                      </Button>
                    </span>
                  </div>
                  {field.type === "select" || field.type === "multiselect" ? (
                    <Field className="mt-2" label={t("interviews.fieldOptions")}>
                      <Input
                        value={(field.options ?? []).join(", ")}
                        onChange={(event) => {
                          const fields = [...draft.fields];
                          fields[index] = {
                            ...field,
                            options: event.target.value.split(",").map((option) => option.trim()).filter(Boolean),
                          };
                          setDraft({ ...draft, fields });
                        }}
                      />
                    </Field>
                  ) : null}
                </li>
              ))}
            </ul>
            <Button
              className="mt-2"
              size="sm"
              variant="outline"
              onClick={() =>
                setDraft({
                  ...draft,
                  fields: [
                    ...draft.fields,
                    {
                      key: `field_${draft.fields.length + 1}`,
                      label: "",
                      type: "text",
                      required: false,
                      visible_to_reviewer: true,
                      options: [],
                    },
                  ],
                })
              }
            >
              <Plus className="h-3.5 w-3.5" />
              {t("interviews.addField")}
            </Button>
          </div>

          {error ? <ErrorNote>{error}</ErrorNote> : null}

          <div className="flex justify-end gap-2">
            {selectedId ? (
              <Button variant="destructive" onClick={() => void remove()}>
                {t("common.delete")}
              </Button>
            ) : null}
            <Button onClick={() => void save()} disabled={busy}>
              {busy ? t("common.saving") : t("common.save")}
            </Button>
          </div>
        </div>
      ) : null}
    </Dialog>
  );
}
