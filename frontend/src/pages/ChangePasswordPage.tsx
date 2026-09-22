import { useNavigate } from "react-router-dom";
import { ROLE_HOME, useAuth } from "../auth/AuthProvider";
import { Card } from "../ui/primitives";
import { ChangePasswordDialog } from "./ChangePasswordDialog";

export function ChangePasswordPage() {
  const { user, reload } = useAuth();
  const navigate = useNavigate();
  if (!user) return null;
  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/40 p-4">
      <Card className="w-full max-w-sm">
        <ChangePasswordDialog
          open
          forced
          onClose={() => undefined}
          onDone={() => {
            void reload().then(() => navigate(ROLE_HOME[user.role], { replace: true }));
          }}
        />
      </Card>
    </div>
  );
}
