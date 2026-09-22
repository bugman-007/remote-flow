import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, notifyUnauthorized } from "../lib/api";
import type { Role, User } from "../types";

export const ROLE_HOME: Record<Role, string> = {
  maker: "/jd-upload",
  manager: "/resumes",
  reviewer: "/interviews",
};

interface AuthContextValue {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string, asAdmin: boolean) => Promise<{ redirect: string; user: User }>;
  logout: () => Promise<void>;
  reload: () => Promise<User | null>;
  setUser: (user: User | null) => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async (): Promise<User | null> => {
    try {
      const payload = await api.get<{ user: User }>("/auth/me");
      setUser(payload.user);
      return payload.user;
    } catch {
      setUser(null);
      return null;
    }
  }, []);

  useEffect(() => {
    void (async () => {
      await reload();
      setLoading(false);
    })();
  }, [reload]);

  useEffect(() => {
    const onUnauthorized = () => setUser(null);
    window.addEventListener("rf:unauthorized", onUnauthorized);
    return () => window.removeEventListener("rf:unauthorized", onUnauthorized);
  }, []);

  const login = useCallback(async (email: string, password: string, asAdmin: boolean) => {
    const payload = await api.post<{ user: User; redirect: string }>("/auth/login", {
      email,
      password,
      as_admin: asAdmin,
    });
    setUser(payload.user);
    return payload;
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post("/auth/logout");
    } catch {
      /* the session may already be gone */
    }
    setUser(null);
    notifyUnauthorized();
  }, []);

  const value = useMemo(
    () => ({ user, loading, login, logout, reload, setUser }),
    [user, loading, login, logout, reload],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
