import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Plug } from "lucide-react";
import { api, errorMessage } from "../../lib/api";
import { Badge, Button, Card, Checkbox, EmptyState, ErrorNote, Field, Input, Select, Spinner } from "../../ui/primitives";
import { Dialog } from "../../ui/dialog";
import { Table } from "../../ui/table";
import { useToast } from "../../ui/toast";
import { formatDateTime, formatMs } from "../../lib/format";
import { t } from "../../i18n";
import type { Provider } from "../../types";

const TYPES = ["anthropic", "openai", "azure_openai", "google_gemini", "openrouter", "openai_compatible", "mock"];

export function ProvidersTab() {
  const { push } = useToast();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<Provider | null>(null);
  const [creating, setCreating] = useState(false);

  const providers = useQuery({
    queryKey: ["providers"],
    queryFn: () => api.get<{ items: Provider[] }>("/providers"),
  });

  const usage = useQuery({
    queryKey: ["stats", "providers"],
    queryFn: () => api.get<{ items: { provider_id: string; requests: number; tokens: number; errors: number; avg_latency_ms: number | null }[] }>("/stats/providers"),
  });

  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["providers"] });
  const rows = providers.data?.items ?? [];

  const test = async (provider: Provider) => {
    try {
      const result = await api.post<{ ok: boolean; latency_ms: number; model: string }>(`/providers/${provider.id}/test`);
      push({ tone: "success", title: t("settings.providers.testOk", { model: result.model, latency: result.latency_ms }) });
      refresh();
    } catch (error) {
      push({ tone: "error", title: errorMessage(error) });
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">{t("settings.providers.apiKeyHint")}</p>
        <Button size="sm" onClick={() => setCreating(true)}>
          <Plus className="h-3.5 w-3.5" />
          {t("settings.providers.create")}
        </Button>
      </div>

      <Card>
        {providers.isLoading ? (
          <p className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Spinner /> {t("common.loading")}
          </p>
        ) : providers.isError ? (
          <ErrorNote>{errorMessage(providers.error)}</ErrorNote>
        ) : rows.length === 0 ? (
          <EmptyState title={t("settings.providers.empty")} />
        ) : (
          <Table>
            <thead>
              <tr>
                <th>{t("settings.providers.displayName")}</th>
                <th>{t("settings.providers.type")}</th>
                <th>{t("settings.providers.defaultModel")}</th>
                <th>{t("common.status")}</th>
                <th>{t("settings.providers.maxConcurrency")}</th>
                <th>RPM</th>
                <th>{t("settings.providers.lastUsed")}</th>
                <th>{t("settings.providers.usage")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((provider) => {
                const stats = usage.data?.items.find((item) => item.provider_id === provider.id);
                return (
                  <tr key={provider.id} className="hover:bg-accent/40">
                    <td className="font-medium">
                      {provider.display_name}
                      <span className="ml-1 flex flex-wrap gap-1">
                        {provider.is_default ? <Badge tone="success">{t("settings.providers.systemDefault")}</Badge> : null}
                        {provider.is_fallback ? <Badge tone="info">{t("settings.providers.fallback")}</Badge> : null}
                      </span>
                    </td>
                    <td className="text-xs">{provider.type}</td>
                    <td className="text-xs">{provider.default_model ?? "—"}</td>
                    <td>
                      <Badge tone={!provider.is_enabled ? "neutral" : provider.last_status === "error" ? "danger" : "success"}>
                        {!provider.is_enabled
                          ? t("settings.providers.status.disabled")
                          : provider.last_status === "error"
                            ? t("settings.providers.status.error")
                            : t("settings.providers.status.ok")}
                      </Badge>
                    </td>
                    <td>{provider.max_concurrency}</td>
                    <td>{provider.rpm ?? "—"}</td>
                    <td className="text-xs">{provider.last_used_at ? formatDateTime(provider.last_used_at) : "—"}</td>
                    <td className="text-xs">
                      {stats ? `${stats.requests} req · ${stats.tokens} tok · ${formatMs(stats.avg_latency_ms)}` : "—"}
                    </td>
                    <td>
                      <div className="flex items-center justify-end gap-1">
                        <Button size="sm" variant="ghost" onClick={() => void test(provider)}>
                          <Plug className="h-3.5 w-3.5" />
                          {t("settings.providers.test")}
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setEditing(provider)}>
                          {t("common.edit")}
                        </Button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>

      <ProviderDialog
        open={creating || Boolean(editing)}
        provider={editing}
        onClose={() => {
          setCreating(false);
          setEditing(null);
        }}
        onSaved={() => {
          setCreating(false);
          setEditing(null);
          refresh();
        }}
      />
    </div>
  );
}

function ProviderDialog({
  open,
  provider,
  onClose,
  onSaved,
}: {
  open: boolean;
  provider: Provider | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { push } = useToast();
  const [draft, setDraft] = useState({
    type: "anthropic",
    display_name: "",
    api_key: "",
    base_url: "",
    default_model: "",
    max_concurrency: 8,
    rpm: "",
    timeout_s: 600,
    is_enabled: true,
  });
  const [models, setModels] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setDraft({
      type: provider?.type ?? "anthropic",
      display_name: provider?.display_name ?? "",
      api_key: "",
      base_url: provider?.base_url ?? "",
      default_model: provider?.default_model ?? "",
      max_concurrency: provider?.max_concurrency ?? 8,
      rpm: provider?.rpm !== null && provider?.rpm !== undefined ? String(provider.rpm) : "",
      timeout_s: provider?.timeout_s ?? 600,
      is_enabled: provider?.is_enabled ?? true,
    });
    setError(null);
    setModels([]);
  }, [open, provider]);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const payload = {
        ...draft,
        api_key: draft.api_key || undefined,
        base_url: draft.base_url || null,
        default_model: draft.default_model || null,
        rpm: draft.rpm === "" ? null : Number(draft.rpm),
      };
      if (provider) await api.patch(`/providers/${provider.id}`, payload);
      else await api.post("/providers", payload);
      push({ tone: "success", title: t("toast.saved") });
      onSaved();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={provider ? t("common.edit") : t("settings.providers.create")}
      width="max-w-2xl"
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            {t("common.cancel")}
          </Button>
          <Button onClick={() => void submit()} loading={busy} disabled={!draft.display_name}>
            {t("common.save")}
          </Button>
        </>
      }
    >
      <div className="grid gap-3 tablet:grid-cols-2">
        <Field label={t("settings.providers.type")}>
          <Select value={draft.type} onChange={(event) => setDraft({ ...draft, type: event.target.value })}>
            {TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={t("settings.providers.displayName")}>
          <Input value={draft.display_name} onChange={(event) => setDraft({ ...draft, display_name: event.target.value })} />
        </Field>
        <Field label={t("settings.providers.apiKey")} hint={provider?.has_api_key ? t("settings.providers.apiKeySet", { mask: provider.api_key_masked }) : t("settings.providers.apiKeyHint")}>
          <Input type="password" value={draft.api_key} onChange={(event) => setDraft({ ...draft, api_key: event.target.value })} placeholder={provider?.api_key_masked ?? ""} />
        </Field>
        <Field label={t("settings.providers.baseUrl")}>
          <Input value={draft.base_url} onChange={(event) => setDraft({ ...draft, base_url: event.target.value })} />
        </Field>
        <Field label={t("settings.providers.defaultModel")}>
          <span className="flex gap-2">
            <Input value={draft.default_model} onChange={(event) => setDraft({ ...draft, default_model: event.target.value })} list="provider-models" />
            {provider ? (
              <Button
                variant="outline"
                onClick={async () => {
                  try {
                    const result = await api.get<{ models: string[] }>(`/providers/${provider.id}/models`);
                    setModels(result.models ?? []);
                  } catch (error) {
                    push({ tone: "error", title: errorMessage(error) });
                  }
                }}
              >
                {t("settings.providers.fetchModels")}
              </Button>
            ) : null}
          </span>
          <datalist id="provider-models">
            {models.map((model) => (
              <option key={model} value={model} />
            ))}
          </datalist>
        </Field>
        <Field label={t("settings.providers.maxConcurrency")}>
          <Input
            type="number"
            value={draft.max_concurrency}
            onChange={(event) => setDraft({ ...draft, max_concurrency: Number(event.target.value) })}
          />
        </Field>
        <Field label={t("settings.providers.rpm")}>
          <Input type="number" value={draft.rpm} onChange={(event) => setDraft({ ...draft, rpm: event.target.value })} />
        </Field>
        <Field label={t("settings.providers.timeout")}>
          <Input type="number" value={draft.timeout_s} onChange={(event) => setDraft({ ...draft, timeout_s: Number(event.target.value) })} />
        </Field>
        <Checkbox
          label={t("common.enabled")}
          checked={draft.is_enabled}
          onChange={(event) => setDraft({ ...draft, is_enabled: event.target.checked })}
        />
      </div>
      {provider ? (
        <div className="mt-4 flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={async () => {
              try {
                await api.post(`/providers/${provider.id}/set-default`);
                push({ tone: "success", title: t("toast.updated") });
                onSaved();
              } catch (error) {
                push({ tone: "error", title: errorMessage(error) });
              }
            }}
          >
            {t("settings.providers.setDefault")}
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={async () => {
              try {
                await api.post(`/providers/${provider.id}/set-fallback`);
                push({ tone: "success", title: t("toast.updated") });
                onSaved();
              } catch (error) {
                push({ tone: "error", title: errorMessage(error) });
              }
            }}
          >
            {t("settings.providers.setFallback")}
          </Button>
          <Button
            size="sm"
            variant="destructive"
            onClick={async () => {
              if (!window.confirm(t("confirm.deleteProvider", { name: provider.display_name }))) return;
              try {
                await api.del(`/providers/${provider.id}`);
                push({ tone: "success", title: t("toast.updated") });
                onSaved();
              } catch (error) {
                push({ tone: "error", title: errorMessage(error) });
              }
            }}
          >
            {t("common.delete")}
          </Button>
        </div>
      ) : null}
      {error ? (
        <div className="mt-3">
          <ErrorNote>{error}</ErrorNote>
        </div>
      ) : null}
    </Dialog>
  );
}
