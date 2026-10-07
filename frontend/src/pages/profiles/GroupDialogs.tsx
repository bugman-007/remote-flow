import { useEffect, useState } from "react";
import { Pencil, Trash2, X } from "lucide-react";
import { api, errorMessage } from "../../lib/api";
import { t } from "../../i18n";
import { Button, ErrorNote, Field, Input } from "../../ui/primitives";
import { Dialog } from "../../ui/dialog";
import { GroupChip } from "../../components/GroupChip";
import type { Profile, ProfileGroup } from "../../types";

/** PRO-11: "Save Group" - name the selection (an existing name adds to that group). */
export function SaveGroupDialog({
  open,
  profileIds,
  groups,
  onClose,
  onSaved,
}: {
  open: boolean;
  profileIds: string[];
  groups: ProfileGroup[];
  onClose: () => void;
  onSaved: (group: ProfileGroup) => void;
}) {
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setName("");
      setError(null);
    }
  }, [open]);

  const existing = groups.find((group) => group.name.toLowerCase() === name.trim().toLowerCase());

  const save = async () => {
    if (!name.trim()) {
      setError(t("profiles.groups.nameRequired"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const group = await api.post<ProfileGroup>("/profile-groups", { name: name.trim(), profile_ids: profileIds });
      onSaved(group);
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
      title={t("profiles.groups.saveTitle", { count: profileIds.length })}
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            {t("common.cancel")}
          </Button>
          <Button onClick={() => void save()} loading={busy}>
            {t("profiles.groups.save")}
          </Button>
        </>
      }
    >
      <Field label={t("profiles.groups.name")} hint={existing ? t("profiles.groups.addsToExisting") : t("profiles.groups.oneGroupHint")}>
        <Input
          autoFocus
          list="profile-group-names"
          value={name}
          maxLength={120}
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void save();
          }}
        />
        <datalist id="profile-group-names">
          {groups.map((group) => (
            <option key={group.id} value={group.name} />
          ))}
        </datalist>
      </Field>
      {error ? <ErrorNote>{error}</ErrorNote> : null}
    </Dialog>
  );
}

/** PRO-11: rename, delete, and take Profiles out of groups. */
export function ManageGroupsDialog({
  open,
  groups,
  profiles,
  onClose,
  onChanged,
}: {
  open: boolean;
  groups: ProfileGroup[];
  profiles: Profile[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draftName, setDraftName] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const names = new Map(profiles.map((profile) => [profile.id, profile.name]));

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      onChanged();
      return true;
    } catch (caught) {
      setError(errorMessage(caught));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const rename = async (group: ProfileGroup) => {
    if (await run(() => api.patch(`/profile-groups/${group.id}`, { name: draftName.trim() }))) setEditing(null);
  };

  return (
    <Dialog open={open} onClose={onClose} title={t("profiles.groups.manage")} width="max-w-2xl">
      {groups.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("profiles.groups.none")}</p>
      ) : (
        <ul className="divide-y divide-border">
          {groups.map((group) => (
            <li key={group.id} className="space-y-2 py-3">
              <div className="flex flex-wrap items-center gap-2">
                {editing === group.id ? (
                  <>
                    <Input
                      className="h-8 w-56 text-sm"
                      value={draftName}
                      maxLength={120}
                      autoFocus
                      onChange={(event) => setDraftName(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") void rename(group);
                        if (event.key === "Escape") setEditing(null);
                      }}
                    />
                    <Button size="sm" onClick={() => void rename(group)} loading={busy} disabled={!draftName.trim()}>
                      {t("common.save")}
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setEditing(null)}>
                      {t("common.cancel")}
                    </Button>
                  </>
                ) : (
                  <>
                    <GroupChip group={group} />
                    <span className="text-xs text-muted-foreground">{t("profiles.groups.count", { count: group.profile_count })}</span>
                    <span className="flex-1" />
                    <Button
                      size="sm"
                      variant="ghost"
                      title={t("profiles.groups.rename")}
                      onClick={() => {
                        setEditing(group.id);
                        setDraftName(group.name);
                      }}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                      {t("profiles.groups.rename")}
                    </Button>
                    {confirmDelete === group.id ? (
                      <Button
                        size="sm"
                        variant="destructive"
                        loading={busy}
                        onClick={() => void run(() => api.del(`/profile-groups/${group.id}`)).then(() => setConfirmDelete(null))}
                      >
                        {t("profiles.groups.confirmDelete")}
                      </Button>
                    ) : (
                      <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(group.id)}>
                        <Trash2 className="h-3.5 w-3.5" />
                        {t("common.delete")}
                      </Button>
                    )}
                  </>
                )}
              </div>
              {group.profile_ids.length ? (
                <div className="flex flex-wrap gap-1.5">
                  {group.profile_ids.map((profileId) => (
                    <span key={profileId} className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-0.5 text-xs">
                      {names.get(profileId) ?? profileId}
                      <button
                        type="button"
                        className="text-muted-foreground hover:text-foreground"
                        title={t("profiles.groups.removeFrom", { group: group.name })}
                        aria-label={t("profiles.groups.removeFrom", { group: group.name })}
                        disabled={busy}
                        onClick={() => void run(() => api.post(`/profile-groups/${group.id}/remove`, { profile_ids: [profileId] }))}
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </span>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">{t("profiles.groups.empty")}</p>
              )}
            </li>
          ))}
        </ul>
      )}
      {error ? <ErrorNote>{error}</ErrorNote> : null}
    </Dialog>
  );
}
