import { useQuery } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { api, errorMessage } from "../lib/api";
import { Badge, Card, CardHeader, EmptyState, ErrorNote, Spinner } from "../ui/primitives";
import { t } from "../i18n";

interface SharedProfile {
  id: string;
  name?: string | null;
  url?: string | null;
  description?: string | null;
  tags?: string[] | null;
  custom_fields?: Record<string, string> | null;
}

/**
 * PRO-2: the Maker's own profile page. Managers choose which fields are shared
 * (Profiles → Share), so this view only ever shows that subset.
 */
export function MyProfilePage() {
  const profile = useQuery({
    queryKey: ["me", "profile"],
    queryFn: () => api.get<{ profile: SharedProfile | null; assigned_at: string | null }>("/me/profile"),
  });

  if (profile.isLoading) {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <Spinner /> {t("common.loading")}
      </p>
    );
  }
  if (profile.isError) return <ErrorNote>{errorMessage(profile.error)}</ErrorNote>;

  const data = profile.data?.profile;
  const custom = Object.entries(data?.custom_fields ?? {});

  if (!data || (!data.name && !data.description && !data.url && !custom.length)) {
    return <EmptyState title={t("myProfile.empty")} hint={t("myProfile.emptyHint")} />;
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">{t("myProfile.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("myProfile.subtitle")}</p>
      </div>

      <Card>
        <CardHeader title={data.name ?? t("myProfile.title")} />
        {data.url ? (
          <a
            className="inline-flex items-center gap-1 text-sm text-primary underline"
            href={data.url}
            target="_blank"
            rel="noreferrer"
          >
            {data.url}
            <ExternalLink className="h-3 w-3" />
          </a>
        ) : null}
        {data.description ? (
          <p className="mt-3 whitespace-pre-wrap text-sm">{data.description}</p>
        ) : null}
        {data.tags?.length ? (
          <div className="mt-3 flex flex-wrap gap-1">
            {data.tags.map((tag) => (
              <Badge key={tag} tone="outline">
                {tag}
              </Badge>
            ))}
          </div>
        ) : null}
      </Card>

      {custom.length ? (
        <Card>
          <CardHeader title={t("profiles.customFields")} />
          <dl className="grid gap-2 text-sm tablet:grid-cols-2">
            {custom.map(([key, value]) => (
              <div key={key}>
                <dt className="rf-label">{key.replace(/_/g, " ")}</dt>
                <dd className="break-words">{value}</dd>
              </div>
            ))}
          </dl>
        </Card>
      ) : null}
    </div>
  );
}
