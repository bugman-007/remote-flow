import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { t } from "../i18n";
import { Tabs } from "../ui/tabs";
import { useUrlState } from "../lib/useUrlState";
import { ProvidersTab } from "./settings/ProvidersTab";
import { ThemesTab } from "./settings/ThemesTab";
import { GeneralTab } from "./settings/GeneralTab";
import { RetentionTab } from "./settings/RetentionTab";
import { SystemStatusTab } from "./settings/SystemStatusTab";
import type { SystemStatus } from "../types";

const DEFAULTS = { tab: "providers" };

export function SettingsPage() {
  const { state, update } = useUrlState(DEFAULTS);

  const status = useQuery({
    queryKey: ["system-status"],
    queryFn: () => api.get<SystemStatus>("/system/status"),
    refetchInterval: 30_000,
    enabled: state.tab === "status",
  });

  const attention = status.data?.needs_attention_total ?? 0;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">{t("settings.title")}</h1>
      <Tabs
        items={[
          { id: "providers", label: t("settings.tabs.providers") },
          { id: "themes", label: t("settings.tabs.themes") },
          { id: "general", label: t("settings.tabs.general") },
          { id: "retention", label: t("settings.tabs.retention") },
          { id: "status", label: t("settings.tabs.status"), badge: attention },
        ]}
        value={state.tab}
        onChange={(id) => update({ tab: id })}
      />
      {state.tab === "providers" ? <ProvidersTab /> : null}
      {state.tab === "themes" ? <ThemesTab /> : null}
      {state.tab === "general" ? <GeneralTab /> : null}
      {state.tab === "retention" ? <RetentionTab /> : null}
      {state.tab === "status" ? <SystemStatusTab /> : null}
    </div>
  );
}
