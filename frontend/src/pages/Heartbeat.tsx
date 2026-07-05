import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { Button, ErrorText, Spinner, formatDate } from "../components/ui";
import type {
  HeartbeatProjectStatus,
  HeartbeatStatus,
  Task,
} from "../types";

/** Dashboard-side heartbeat overview: a global toggle + per-project
 *  toggles + a "recent heartbeat-spawned tasks" feed.  Polls every 5s so
 *  the UI reflects the next tick within seconds without requiring a
 *  WebSocket for v1.
 */
export default function Heartbeat() {
  const [status, setStatus] = useState<HeartbeatStatus | null>(null);
  const [recent, setRecent] = useState<Task[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [s, r] = await Promise.all([
        api.getHeartbeat(),
        fetchRecentHeartbeatTasks(),
      ]);
      setStatus(s);
      setRecent(r);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, 5000);
    return () => window.clearInterval(id);
  }, [refresh]);

  async function flipGlobal(enabled: boolean) {
    setBusy(true);
    try {
      await api.setHeartbeatEnabled(enabled);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function flipProject(projectId: string, enabled: boolean) {
    setBusy(true);
    try {
      await api.setProjectHeartbeatEnabled(projectId, enabled);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function triggerNow() {
    setBusy(true);
    try {
      await api.triggerHeartbeat();
      // Poll once quickly so the UI picks up the just-fired tick.
      window.setTimeout(refresh, 1500);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (!status) {
    return (
      <div className="flex h-64 items-center justify-center text-slate-400">
        <Spinner className="h-6 w-6" />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl space-y-8 p-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold text-slate-100">
          🤖 Heartbeat
        </h1>
        <p className="text-sm text-slate-400">
          Prüft regelmäßig die offenen GitHub-Issues der aktiven Projekte und
          startet für jedes neue Issue automatisch einen {status.agent_key}-Task,
          der das Problem untersucht und als PR mit dem Titel
          <code className="ml-1 rounded bg-slate-800 px-1 py-0.5 text-xs">
            Fix #N: …
          </code>{" "}
          zurückgibt.
        </p>
      </header>

      {error && <ErrorText>{error}</ErrorText>}

      <section className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <span
              className={`inline-flex h-3 w-3 rounded-full ${
                status.enabled
                  ? "bg-emerald-400 shadow-[0_0_8px_rgba(16,185,129,0.5)]"
                  : "bg-slate-600"
              }`}
              aria-hidden
            />
            <span className="text-base font-medium text-slate-100">
              {status.enabled ? "Heartbeat aktiv" : "Heartbeat pausiert"}
            </span>
            <span className="text-xs text-slate-400">
              · Intervall alle {Math.round(status.interval_seconds / 60)} Min
              · Cooldown {status.cooldown_minutes} Min pro Projekt
            </span>
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              disabled={busy}
              onClick={triggerNow}
              title="Einmal sofort einen Tick ausführen (fire-and-forget)"
            >
              ▶ Run now
            </Button>
            {status.enabled ? (
              <Button
                variant="subtle"
                disabled={busy}
                onClick={() => flipGlobal(false)}
              >
                Pausieren
              </Button>
            ) : (
              <Button
                variant="primary"
                disabled={busy}
                onClick={() => flipGlobal(true)}
              >
                Aktivieren
              </Button>
            )}
          </div>
        </div>
        <p className="mt-3 text-xs text-slate-500">
          Der globale Schalter gilt nur im laufenden Prozess. Für einen
          dauerhaften Default setze{" "}
          <code className="rounded bg-slate-800 px-1 py-0.5">
            CD_HEARTBEAT_ENABLED=true
          </code>{" "}
          in der Service-Konfiguration und starte neu.
        </p>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-medium text-slate-100">Projekte</h2>
        {status.projects.length === 0 ? (
          <p className="rounded-2xl border border-slate-800 bg-slate-900/40 p-6 text-sm text-slate-400">
            Keine aktiven Projekte mit GitHub-Verknüpfung. Importiere ein
            Repo auf der Startseite, um es hier zu sehen.
          </p>
        ) : (
          <div className="overflow-hidden rounded-2xl border border-slate-800">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-900/70 text-xs uppercase tracking-wide text-slate-400">
                <tr>
                  <th className="px-4 py-2.5">Projekt</th>
                  <th className="px-4 py-2.5">Repo</th>
                  <th className="px-4 py-2.5">Letzter Tick</th>
                  <th className="px-4 py-2.5">Status</th>
                  <th className="px-4 py-2.5">Offen</th>
                  <th className="px-4 py-2.5 text-right">Aktion</th>
                </tr>
              </thead>
              <tbody>
                {status.projects.map((p) => (
                  <ProjectRow
                    key={p.id}
                    p={p}
                    busy={busy}
                    onToggle={(enabled) => flipProject(p.id, enabled)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-medium text-slate-100">
          Zuletzt automatisch gestartete Tasks
        </h2>
        {recent.length === 0 ? (
          <p className="rounded-2xl border border-slate-800 bg-slate-900/40 p-6 text-sm text-slate-400">
            Noch keine Heartbeat-Tasks. Sobald ein neues Issue auftaucht,
            taucht hier der zugehörige Fix-Versuch auf.
          </p>
        ) : (
          <ul className="divide-y divide-slate-800 overflow-hidden rounded-2xl border border-slate-800">
            {recent.map((t) => (
              <li key={t.id} className="bg-slate-900/40 px-4 py-3 text-sm">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <a
                    href={`#/projects/${t.project_id}`}
                    className="font-medium text-cyan-300 hover:text-cyan-200"
                  >
                    🤖 Fix #{t.heartbeat_issue_number ?? "?"}
                  </a>
                  <span className="text-xs text-slate-500">
                    {formatDate(t.created_at)}
                  </span>
                </div>
                <p className="mt-1 line-clamp-2 text-xs text-slate-400">
                  {t.result_summary || t.prompt.slice(0, 240)}
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  Agent: <code className="text-slate-300">{t.agent}</code> ·
                  Status:{" "}
                  <code className="text-slate-300">{t.status}</code>
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function ProjectRow({
  p,
  busy,
  onToggle,
}: {
  p: HeartbeatProjectStatus;
  busy: boolean;
  onToggle: (enabled: boolean) => void;
}) {
  const statusLabel = heartbeatStatusLabel(p);
  const statusColor = heartbeatStatusColor(p.last_heartbeat_status);
  return (
    <tr className="border-t border-slate-800 align-top">
      <td className="px-4 py-3">
        <a
          href={`#/projects/${p.id}`}
          className="font-medium text-slate-100 hover:text-cyan-300"
        >
          {p.name}
        </a>
      </td>
      <td className="px-4 py-3 text-xs text-slate-400">
        {p.github_full_name || (
          <span className="italic text-slate-600">kein GitHub</span>
        )}
      </td>
      <td className="px-4 py-3 text-xs text-slate-400">
        {formatDate(p.last_heartbeat_at)}
      </td>
      <td className="px-4 py-3">
        <span
          className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs ${statusColor}`}
          title={p.last_heartbeat_error || undefined}
        >
          {statusLabel}
        </span>
        {p.last_heartbeat_error && (
          <p className="mt-1 max-w-xs truncate text-xs text-red-300">
            {p.last_heartbeat_error}
          </p>
        )}
      </td>
      <td className="px-4 py-3 text-xs text-slate-400">
        {p.inflight_task_ids.length > 0 ? (
          <span className="font-mono text-amber-300">
            {p.inflight_task_ids.length} laufend
          </span>
        ) : (
          <span className="text-slate-600">–</span>
        )}
      </td>
      <td className="px-4 py-3 text-right">
        <button
          type="button"
          disabled={busy}
          onClick={() => onToggle(!p.enabled)}
          className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
            p.enabled ? "bg-cyan-500" : "bg-slate-700"
          } ${busy ? "opacity-50" : ""}`}
          aria-pressed={p.enabled}
          title={p.enabled ? "Heartbeat für dieses Projekt deaktivieren" : "Heartbeat für dieses Projekt aktivieren"}
        >
          <span
            className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
              p.enabled ? "translate-x-6" : "translate-x-1"
            }`}
          />
        </button>
      </td>
    </tr>
  );
}

function heartbeatStatusLabel(p: HeartbeatProjectStatus): string {
  if (!p.enabled) return "Aus";
  if (!p.github_full_name) return "Kein Repo";
  if (!p.last_heartbeat_status) return "Noch nicht geprüft";
  const map: Record<string, string> = {
    success: "Erfolg",
    no_issues: "Keine neuen Issues",
    cooldown: "Cooldown",
    disabled: "Aus",
    error: "Fehler",
    skipped: "Übersprungen",
    no_github: "Kein Repo",
  };
  return map[p.last_heartbeat_status] ?? p.last_heartbeat_status;
}

function heartbeatStatusColor(status: string): string {
  const map: Record<string, string> = {
    success: "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40",
    no_issues: "bg-slate-700 text-slate-300",
    cooldown: "bg-amber-500/20 text-amber-300 border border-amber-500/40",
    disabled: "bg-slate-800 text-slate-500",
    error: "bg-red-500/20 text-red-300 border border-red-500/40",
  };
  return map[status] ?? "bg-slate-800 text-slate-400";
}

/** Fetch the last 20 heartbeat-spawned tasks across all projects. There
 *  isn't a dedicated endpoint yet — we hit /running and the per-project
 *  /tasks lists. To keep the request count low in v1 we walk the running
 *  list plus a single /tasks call per active project (cheap because the
 *  per-project call returns a flat list ordered by created_at desc).
 *
 *  For a future iteration this should become a dedicated
 *  ``GET /api/heartbeat/recent-tasks`` endpoint.
 */
async function fetchRecentHeartbeatTasks(): Promise<Task[]> {
  try {
    const running = await api.listRunning();
    const heartbeatRunning = running.filter((t) => t.heartbeat_spawned);
    return heartbeatRunning.slice(0, 20);
  } catch {
    return [];
  }
}