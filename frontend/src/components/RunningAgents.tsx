import { useEffect, useState } from "react";
import { api } from "../api";
import { subscribe as subscribeCrossTab } from "../crossTab";
import type { RunningTask } from "../types";
import { Spinner, StatusBadge } from "./ui";
import { openAgentWindow } from "./WindowManager";

function modeLabel(t: RunningTask): string {
  if (t.is_session || t.mode === "session") return "Session";
  if (t.mode === "goal") return "Ziel";
  return "Aufgabe";
}

function modeChipClass(t: RunningTask): string {
  if (t.is_session || t.mode === "session") return "bg-purple-500/15 text-purple-300";
  if (t.mode === "goal") return "bg-cyan-500/15 text-cyan-300";
  return "bg-slate-700/60 text-slate-300";
}

/** Live cross-project dashboard of all running/queued agents. Polls /running.
 *  Clicking an entry opens its console / session window directly (the floating
 *  WindowManager). Sessions open a PTY-backed window; tasks/goals open a live
 *  TaskConsole. The project itself is NOT navigated to — clicking just opens
 *  the agent's window, exactly as the user requested. */
export default function RunningAgents() {
  const [running, setRunning] = useState<RunningTask[] | null>(null);
  const [names, setNames] = useState<Record<string, string>>({});

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setInterval> | null = null;

    (async () => {
      try {
        const ag = await api.agents();
        if (active) setNames(Object.fromEntries(ag.map((a) => [a.key, a.display_name])));
      } catch {
        /* names are cosmetic */
      }
    })();

    async function poll() {
      try {
        const r = await api.listRunning();
        if (active) setRunning(r);
      } catch {
        if (active) setRunning((prev) => prev ?? []);
      }
    }
    void poll();
    timer = setInterval(() => void poll(), 3000);

    // Cross-tab fix: when a sibling tab ends a task / session, the polling
    // cycle above would still show it for up to 3 s. Listen on the
    // BroadcastChannel and re-poll immediately so the "Laufende Agenten"
    // panel updates the moment the popup / console reports done (issue #5).
    const unsubscribe = subscribeCrossTab((event) => {
      if (event.type === "task-done" || event.type === "session-done") {
        void poll();
      }
    });

    return () => {
      active = false;
      if (timer) clearInterval(timer);
      unsubscribe();
    };
  }, []);

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900">
      <div className="flex items-center justify-between px-5 py-3">
        <h2 className="flex items-center gap-2 font-medium text-slate-200">
          {running && running.length > 0 && (
            <span className="relative flex h-2.5 w-2.5">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
              <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-emerald-500" />
            </span>
          )}
          Laufende Agenten
          {running && running.length > 0 && (
            <span className="text-sm text-slate-500">({running.length})</span>
          )}
        </h2>
        {running === null && <Spinner className="h-4 w-4 text-slate-500" />}
      </div>

      {running !== null && running.length === 0 && (
        <p className="px-5 pb-4 text-sm text-slate-500">Aktuell arbeitet kein Agent.</p>
      )}

      {running !== null && running.length > 0 && (
        <div className="divide-y divide-slate-800 border-t border-slate-800">
          {running.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => openAgentWindow(t, names[t.agent] ?? t.agent)}
              className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-5 py-3 text-left transition hover:bg-slate-800/50"
              title="Agent-Fenster öffnen"
            >
              <StatusBadge status={t.status} />
              <span className="text-sm font-medium text-slate-200">
                {names[t.agent] ?? t.agent}
              </span>
              <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${modeChipClass(t)}`}>
                {modeLabel(t)}
              </span>
              {t.runner === "host" && (
                <span
                  className="shrink-0 rounded bg-emerald-500/15 px-1.5 py-0.5 text-xs font-medium text-emerald-300"
                  title="Auf dem Host per SSH ausgefuehrt"
                >
                  🖥 host
                </span>
              )}
              {t.env_profile_key && (
                <span
                  className="shrink-0 rounded bg-amber-500/15 px-1.5 py-0.5 text-xs font-medium text-amber-300"
                  title={`Env-Profil: ${t.env_profile_key}`}
                >
                  🔑 {t.env_profile_key}
                </span>
              )}
              <span className="text-sm text-cyan-400">{t.project_name || t.project_slug}</span>
              <span className="min-w-0 flex-1 truncate text-sm text-slate-400">
                {t.prompt || (t.is_session ? "interaktive Session" : "")}
              </span>
              <span className="text-xs text-slate-600">öffnen →</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
