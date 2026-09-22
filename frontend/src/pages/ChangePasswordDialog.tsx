import { useState, type FormEvent } from "react";
import { Dialog } from "../ui/dialog";
import { Button, ErrorNote, Field, Input } from "../ui/primitives";
import { api, errorMessage } from "../lib/api";
import { useToast } from "../ui/toast";
import { t } from "../i18n";

export function ChangePasswordDialog({
  open,
  forced,
  onClose,
  onDone,
}: {
  open: boolean;
  forced: boolean;
  onClose: () => void;
  onDone: () => void;
}) {
  const { push } = useToast();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (next.length < 10) {
      setError(t("password.tooShort"));
      return;
    }
    if (next !== confirm) {
      setError(t("password.mismatch"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.post("/auth/change-password", { current_password: current, new_password: next });
      push({ tone: "success", title: t("password.changed") });
      setCurrent("");
      setNext("");
      setConfirm("");
      onDone();
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
      title={t("password.changeTitle")}
      description={forced ? t("password.forced") : undefined}
      width="max-w-sm"
    >
      <form onSubmit={submit}>
        <Field label={t("password.current")}>
          <Input type="password" autoComplete="current-password" required value={current} onChange={(event) => setCurrent(event.target.value)} />
        </Field>
        <Field label={t("password.next")}>
          <Input type="password" autoComplete="new-password" required value={next} onChange={(event) => setNext(event.target.value)} />
        </Field>
        <Field label={t("password.confirm")}>
          <Input type="password" autoComplete="new-password" required value={confirm} onChange={(event) => setConfirm(event.target.value)} />
        </Field>
        {error ? (
          <div className="mb-3">
            <ErrorNote>{error}</ErrorNote>
          </div>
        ) : null}
        <div className="flex justify-end gap-2">
          {forced ? null : (
            <Button variant="outline" onClick={onClose} disabled={busy}>
              {t("common.cancel")}
            </Button>
          )}
          <Button type="submit" loading={busy}>
            {t("password.submit")}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
