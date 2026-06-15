import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { RunningTask } from "../types";
import { Spinner, StatusBadge } from "./ui";

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

/** Live cross-project dashboard of all running/queued agents. Polls /running. */
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
    return () => {
      active = false;
      if (timer) clearInterval(timer);
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
            <Link
              key={t.id}
              to={`/projects/${t.project_id}`}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-3 transition hover:bg-slate-800/50"
            >
              <StatusBadge status={t.status} />
              <span className="text-sm font-medium text-slate-200">
                {names[t.agent] ?? t.agent}
              </span>
              <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${modeChipClass(t)}`}>
                {modeLabel(t)}
              </span>
              <span className="text-sm text-cyan-400">{t.project_name || t.project_slug}</span>
              <span className="min-w-0 flex-1 truncate text-sm text-slate-400">
                {t.prompt || (t.is_session ? "interaktive Session" : "")}
              </span>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
