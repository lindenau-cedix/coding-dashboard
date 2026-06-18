import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api, getToken, setToken } from "./api";

interface AuthState {
  token: string | null;
  username: string | null;
  ready: boolean;
  // False when the backend runs without auth (e.g. behind a Cloudflare tunnel):
  // no login screen, every request is allowed.
  authRequired: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTok] = useState<string | null>(getToken());
  const [username, setUsername] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [authRequired, setAuthRequired] = useState(true);

  const logout = useCallback(() => {
    setToken(null);
    setTok(null);
    setUsername(null);
  }, []);

  useEffect(() => {
    let active = true;
    (async () => {
      // Find out whether the backend enforces auth at all.
      let required = true;
      try {
        const status = await api.authStatus();
        required = status.auth_required;
      } catch {
        /* default to requiring auth if the probe fails */
      }
      if (!active) return;
      setAuthRequired(required);

      if (!required) {
        // No auth -> resolve the user from /me (returns the single admin user).
        try {
          const me = await api.me();
          if (active) setUsername(me.username);
        } catch {
          /* ignore -- access is allowed regardless */
        }
        if (active) setReady(true);
        return;
      }

      if (getToken()) {
        try {
          const me = await api.me();
          if (active) setUsername(me.username);
        } catch {
          if (active) logout();
        }
      }
      if (active) setReady(true);
    })();
    return () => {
      active = false;
    };
  }, [logout]);

  // Global 401 handler dispatched by the api layer.
  useEffect(() => {
    const handler = () => logout();
    window.addEventListener("cd-unauthorized", handler);
    return () => window.removeEventListener("cd-unauthorized", handler);
  }, [logout]);

  const login = useCallback(async (u: string, p: string) => {
    const res = await api.login(u, p);
    setToken(res.access_token);
    setTok(res.access_token);
    setUsername(res.username);
  }, []);

  const value = useMemo<AuthState>(
    () => ({ token, username, ready, authRequired, login, logout }),
    [token, username, ready, authRequired, login, logout],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
