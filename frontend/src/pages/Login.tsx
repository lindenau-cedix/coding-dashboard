import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { getApiBase, setApiBase } from "../api";
import { useAuth } from "../auth";
import { Button, ErrorText } from "../components/ui";

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const [showServer, setShowServer] = useState(false);
  const [apiBase, setApiBaseInput] = useState(getApiBase());

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      setApiBase(apiBase);
      await login(username, password);
      navigate("/", { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login fehlgeschlagen");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center px-4 py-12">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm space-y-4 rounded-2xl border border-slate-800 bg-slate-900 p-6 shadow-xl"
      >
        <div className="text-center">
          <div className="text-3xl text-cyan-400">⌘</div>
          <h1 className="mt-2 text-xl font-semibold text-slate-100">Coding Dashboard</h1>
          <p className="text-sm text-slate-400">Bitte anmelden</p>
        </div>

        <ErrorText>{error}</ErrorText>

        <div className="space-y-1">
          <label className="text-sm text-slate-300">Benutzername</label>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-slate-100 outline-none focus:border-cyan-500"
          />
        </div>
        <div className="space-y-1">
          <label className="text-sm text-slate-300">Passwort</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-slate-100 outline-none focus:border-cyan-500"
          />
        </div>

        <Button type="submit" disabled={busy} className="w-full">
          {busy ? "Anmelden…" : "Anmelden"}
        </Button>

        <button
          type="button"
          onClick={() => setShowServer((s) => !s)}
          className="w-full text-center text-xs text-slate-500 hover:text-slate-300"
        >
          {showServer ? "▲ Server-Einstellungen" : "▼ Server-Einstellungen"}
        </button>
        {showServer && (
          <div className="space-y-1 rounded-lg border border-slate-800 bg-slate-950/50 p-3">
            <label className="text-xs text-slate-400">
              Backend-URL (leer = gleiche Herkunft wie diese Seite)
            </label>
            <input
              value={apiBase}
              onChange={(e) => setApiBaseInput(e.target.value)}
              placeholder="https://dashboard.example.com"
              className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-500"
            />
            <p className="text-xs text-slate-500">
              In der Android-App hier die öffentliche Adresse deines Servers eintragen.
            </p>
          </div>
        )}
      </form>
    </div>
  );
}
