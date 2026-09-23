import { Navigate, Route, Routes } from "react-router-dom";
import { RequireAuth, RedirectHome } from "./auth/guards";
import { AppShell } from "./layout/AppShell";
import { LoginPage } from "./pages/LoginPage";
import { ChangePasswordPage } from "./pages/ChangePasswordPage";
import { JdUploadPage } from "./pages/JdUploadPage";
import { MyProfilePage } from "./pages/MyProfilePage";
import { ResumesPage } from "./pages/ResumesPage";
import { InterviewsPage } from "./pages/InterviewsPage";
import { ProfilesPage } from "./pages/ProfilesPage";
import { SettingsPage } from "./pages/SettingsPage";
import { UsersPage } from "./pages/UsersPage";

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/change-password" element={<ChangePasswordPage />} />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route path="/jd-upload" element={<RequireAuth roles={["maker"]}><JdUploadPage /></RequireAuth>} />
        <Route path="/my-profile" element={<RequireAuth roles={["maker"]}><MyProfilePage /></RequireAuth>} />
        <Route path="/resumes" element={<RequireAuth roles={["maker", "manager"]}><ResumesPage /></RequireAuth>} />
        <Route path="/interviews" element={<RequireAuth roles={["manager", "reviewer"]}><InterviewsPage /></RequireAuth>} />
        <Route path="/profiles" element={<RequireAuth roles={["manager"]}><ProfilesPage /></RequireAuth>} />
        <Route path="/settings" element={<RequireAuth roles={["manager"]}><SettingsPage /></RequireAuth>} />
        <Route path="/users" element={<RequireAuth roles={["manager"]}><UsersPage /></RequireAuth>} />
      </Route>
      <Route path="/" element={<RedirectHome />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
