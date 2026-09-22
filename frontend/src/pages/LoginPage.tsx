import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { KeyRound } from "lucide-react";
import { ROLE_HOME, useAuth } from "../auth/AuthProvider";
import { Button, Card, ErrorNote, Field, Input } from "../ui/primitives";
import { errorMessage } from "../lib/api";
import { t } from "../i18n";

export function LoginPage() {
  const { user, loading, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"login" | "admin" | null>(null);

  if (!loading && user) {
    const home = user.must_change_password ? "/change-password" : ROLE_HOME[user.role];
    const from = (location.state as { from?: string } | null)?.from;
    return <Navigate to={from ?? home} replace />;
  }

  const submit = async (asAdmin: boolean) => {
    setBusy(asAdmin ? "admin" : "login");
    setError(null);
    try {
      const result = await login(email.trim(), password, asAdmin);
      const target = result.user.must_change_password
        ? "/change-password"
        : result.redirect || ROLE_HOME[result.user.role];
      navigate(target, { replace: true });
    } catch (caught) {
      setError(mapLoginError(caught));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/40 p-4">
      <Card className="w-full max-w-sm">
        <div className="mb-4 flex items-center gap-2">
          <span className="flex h-9 w-9 items-center justify-center rounded-full bg-primary/10 text-primary">
            <KeyRound className="h-4 w-4" />
          </span>
          <div>
            <h1 className="text-sm font-semibold">{t("login.title")}</h1>
            <p className="text-xs text-muted-foreground">{t("login.subtitle")}</p>
          </div>
        </div>

        <form
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            void submit(false);
          }}
        >
          <Field label={t("login.email")}>
            <Input
              type="email"
              autoComplete="username"
              autoFocus
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </Field>
          <Field label={t("login.password")}>
            <Input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </Field>
          {error ? (
            <div className="mb-3">
              <ErrorNote>{error}</ErrorNote>
            </div>
          ) : null}
          <div className="flex flex-col gap-2">
            <Button type="submit" disabled={busy !== null}>
              {busy === "login" ? t("login.signingIn") : t("login.submit")}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={busy !== null}
              onClick={() => void submit(true)}
            >
              {busy === "admin" ? t("login.signingIn") : t("login.submitAdmin")}
            </Button>
          </div>
        </form>
        <p className="mt-4 text-center text-xs text-muted-foreground">{t("login.contact")}</p>
      </Card>
    </div>
  );
}

function mapLoginError(error: unknown): string {
  const message = errorMessage(error);
  if (message === "Email or password is incorrect.") return t("login.failed");
  if (message === "This account is not a manager.") return message;
  return message;
}
