// Auth state for the whole app: the token, and who it belongs to.
//
// On load, if a token is already stored, we ask /v1/auth/me who it is. A 401 means the token
// is stale (expired, or the server restarted with a new secret) - so we clear it and show the
// login screen, rather than leaving the UI in a half-logged-in limbo.

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api, setToken } from "./api";

export type Me = {
  user_id: string;
  tenant_id: string;
  tenant_slug: string;
  email: string;
  role: string;
};

type AuthState = {
  me: Me | null;
  loading: boolean;
  login: (tenantSlug: string, email: string, password: string) => Promise<void>;
  logout: () => void;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const who = await api<Me>("/v1/auth/me");
        if (alive) setMe(who);
      } catch {
        setToken(null); // stale or absent token: start clean
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  async function login(tenantSlug: string, email: string, password: string) {
    const { access_token } = await api<{ access_token: string }>("/v1/auth/login", {
      method: "POST",
      body: { tenant_slug: tenantSlug, email, password },
    });
    setToken(access_token);
    setMe(await api<Me>("/v1/auth/me"));
  }

  function logout() {
    setToken(null);
    setMe(null);
  }

  return (
    <AuthContext.Provider value={{ me, loading, login, logout }}>{children}</AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
