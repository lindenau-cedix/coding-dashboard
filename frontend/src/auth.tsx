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
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTok] = useState<string | null>(getToken());
  const [username, setUsername] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  const logout = useCallback(() => {
    setToken(null);
    setTok(null);
    setUsername(null);
  }, []);

  useEffect(() => {
    let active = true;
    (async () => {
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
    () => ({ token, username, ready, login, logout }),
    [token, username, ready, login, logout],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
