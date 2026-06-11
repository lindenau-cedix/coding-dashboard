import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, commitUrl } from "../api";
import TaskConsole from "../components/TaskConsole";
import { Button, ErrorText, Modal, Spinner, StatusBadge, formatDate } from "../components/ui";
import type { Agent, Project, Task, TaskMode } from "../types";

export default function ProjectDetail() {
  const { id = "" } = useParams();
  const [project, setProject] = useState<Project | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const [agent, setAgent] = useState("");
  const [mode, setMode] = useState<TaskMode>("task");
  const [prompt, setPrompt] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);

  const [expanded, setExpanded] = useState<string | null>(null);
  const [outputs, setOutputs] = useState<Record<string, string>>({});

  const [showAgentsMd, setShowAgentsMd] = useState(false);
  const [agentsMd, setAgentsMd] = useState<string | null>(null);
  const [pulling, setPulling] = useState(false);
  const [pullDialog, setPullDialog] = useState<{ open: boolean; output: string; success: boolean }>({
    open: false,
    output: "",
    success: false,
  });

  const agentName = useMemo(() => {
    const m: Record<string, string> = {};
    for (const a of agents) m[a.key] = a.display_name;
    return m;
  }, [agents]);

  const goalSupported = useMemo(() => agents.some((a) => a.supports_goal), [agents]);
  // In goal mode only agents that support it are selectable.
  const selectableAgents = useMemo(
    () => (mode === "goal" ? agents.filter((a) => a.supports_goal) : agents),
    [agents, mode],
  );

  function changeMode(next: TaskMode) {
    setMode(next);
    if (next === "goal") {
      const current = agents.find((a) => a.key === agent);
      if (!current || !current.supports_goal) {
        const first = agents.find((a) => a.supports_goal && a.enabled)
          ?? agents.find((a) => a.supports_goal);
        if (first) setAgent(first.key);
      }
    }
  }

  async function refreshTasks() {
    setTasks(await api.listTasks(id));
  }

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const [p, ag, ts] = await Promise.all([
          api.getProject(id),
          api.agents(),
          api.listTasks(id),
        ]);
        if (!active) return;
        setProject(p);
        setAgents(ag);
        setTasks(ts);
        const firstEnabled = ag.find((a) => a.enabled);
        if (firstEnabled) setAgent(firstEnabled.key);
        const live = ts.find((t) => t.status === "running" || t.status === "queued");
        if (live) setActiveTaskId(live.id);
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : "Laden fehlgeschlagen");
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [id]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!agent || !prompt.trim()) return;
    setSubmitting(true);
    setError("");
    try {
      const task = await api.createTask(id, agent, prompt.trim(), mode);
      setActiveTaskId(task.id);
      setPrompt("");
      await refreshTasks();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Task konnte nicht gestartet werden");
    } finally {
      setSubmitting(false);
    }
  }

  async function toggleExpand(task: Task) {
    if (expanded === task.id) {
      setExpanded(null);
      return;
    }
    setExpanded(task.id);
    if (outputs[task.id] === undefined) {
      try {
        const full = await api.getTask(task.id);
        setOutputs((o) => ({ ...o, [task.id]: full.output ?? "" }));
      } catch {
        setOutputs((o) => ({ ...o, [task.id]: "(Ausgabe konnte nicht geladen werden)" }));
      }
    }
  }

  async function toggleAgentsMd() {
    const next = !showAgentsMd;
    setShowAgentsMd(next);
    if (next && agentsMd === null) {
      try {
        const res = await api.agentsMd(id);
        setAgentsMd(res.exists ? res.content : "(AGENTS.md existiert noch nicht)");
      } catch {
        setAgentsMd("(AGENTS.md konnte nicht geladen werden)");
      }
    }
  }

  async function pull() {
    setPulling(true);
    try {
      const res = await api.pullProject(id);
      setPullDialog({ open: true, output: res.output || "Erfolgreich gepullt.", success: true });
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Pull fehlgeschlagen";
      setPullDialog({ open: true, output: msg, success: false });
    } finally {
      setPulling(false);
    }
  }

  if (loading) {
    return (
      <div className="flex justify-center py-16 text-slate-500">
        <Spinner className="h-6 w-6" />
      </div>
    );
  }
  if (!project) {
    return <ErrorText>{error || "Projekt nicht gefunden."}</ErrorText>;
  }

  return (
    <div className="space-y-6">
      <div>
        <Link to="/" className="text-sm text-slate-400 hover:text-cyan-400">
          ← Projekte
        </Link>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
          <h1 className="text-2xl font-semibold text-slate-100">{project.name}</h1>
          <div className="flex items-center gap-3 text-sm text-slate-400">
            {project.github_full_name && (
              <a
                href={project.github_url}
                target="_blank"
                rel="noreferrer"
                className="hover:text-cyan-400"
              >
                {project.github_full_name}
              </a>
            )}
            <span className="rounded bg-slate-800 px-2 py-0.5 text-xs">
              ⎇ {project.default_branch}
            </span>
            <button
              onClick={pull}
              disabled={pulling}
              className="rounded border border-cyan-700 bg-cyan-900/30 px-2.5 py-1 text-xs font-medium text-cyan-400 transition hover:bg-cyan-900/60 disabled:opacity-50"
            >
              {pulling ? "Pulling…" : "Pull"}
            </button>
          </div>
        </div>
        {project.description && (
          <p className="mt-1 text-sm text-slate-400">{project.description}</p>
        )}
      </div>

      <ErrorText>{error}</ErrorText>

      {/* New task */}
      <form
        onSubmit={submit}
        className="space-y-3 rounded-2xl border border-slate-800 bg-slate-900 p-5"
      >
        <h2 className="font-medium text-slate-200">
          {mode === "goal" ? "Neues Ziel" : "Neue Aufgabe"}
        </h2>
        {goalSupported && (
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-sm text-slate-400">Modus:</label>
            <div className="inline-flex overflow-hidden rounded-lg border border-slate-700">
              {(["task", "goal"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => changeMode(m)}
                  className={`px-3 py-1.5 text-sm transition ${
                    mode === m
                      ? "bg-cyan-600 text-white"
                      : "bg-slate-800 text-slate-300 hover:bg-slate-700"
                  }`}
                >
                  {m === "goal" ? "Ziel (/goal)" : "Aufgabe"}
                </button>
              ))}
            </div>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-sm text-slate-400">Agent:</label>
          <select
            value={agent}
            onChange={(e) => setAgent(e.target.value)}
            className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-cyan-500"
          >
            {selectableAgents.map((a) => (
              <option key={a.key} value={a.key} disabled={!a.enabled}>
                {a.display_name}
                {a.enabled ? "" : " (deaktiviert)"}
              </option>
            ))}
          </select>
        </div>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={4}
          placeholder={
            mode === "goal"
              ? "Beschreibe das Ziel – der Agent arbeitet im /goal-Modus, bis es erreicht ist…"
              : "Beschreibe die Aufgabe, die der Agent im Projekt erledigen soll…"
          }
          className="w-full resize-y rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-500"
        />
        <div className="flex items-center justify-between">
          <p className="text-xs text-slate-500">
            {mode === "goal"
              ? "Der gesamte Verlauf bis zum Ziel zählt als ein Task. Änderungen werden danach automatisch committet & gepusht."
              : "Nach Abschluss werden Änderungen automatisch committet & gepusht."}
          </p>
          <Button type="submit" disabled={submitting || !agent || !prompt.trim()}>
            {submitting
              ? "Startet…"
              : mode === "goal"
                ? "Ziel starten"
                : "Aufgabe starten"}
          </Button>
        </div>
      </form>

      {/* Live console */}
      {activeTaskId && (
        <div className="space-y-2">
          <h2 className="font-medium text-slate-200">Live-Ausgabe</h2>
          <TaskConsole
            taskId={activeTaskId}
            onDone={() => {
              void refreshTasks();
            }}
          />
        </div>
      )}

      {/* AGENTS.md */}
      <div className="rounded-2xl border border-slate-800 bg-slate-900">
        <button
          onClick={toggleAgentsMd}
          className="flex w-full items-center justify-between px-5 py-3 text-left text-sm font-medium text-slate-200"
        >
          <span>AGENTS.md (gemeinsamer Kontext für Claude & Hermes)</span>
          <span className="text-slate-500">{showAgentsMd ? "▲" : "▼"}</span>
        </button>
        {showAgentsMd && (
          <pre className="max-h-80 overflow-auto border-t border-slate-800 p-4 font-mono text-xs whitespace-pre-wrap text-slate-300">
            {agentsMd ?? "Lädt…"}
          </pre>
        )}
      </div>

      {/* History */}
      <div className="space-y-2">
        <h2 className="font-medium text-slate-200">Historie ({tasks.length})</h2>
        {tasks.length === 0 ? (
          <p className="text-sm text-slate-500">Noch keine Aufgaben.</p>
        ) : (
          <div className="space-y-2">
            {tasks.map((t) => (
              <div key={t.id} className="rounded-xl border border-slate-800 bg-slate-900">
                <button
                  onClick={() => toggleExpand(t)}
                  className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 text-left"
                >
                  <StatusBadge status={t.status} />
                  <span className="text-sm font-medium text-slate-200">
                    {agentName[t.agent] ?? t.agent}
                  </span>
                  {t.mode === "goal" && (
                    <span className="rounded bg-cyan-500/15 px-1.5 py-0.5 text-xs font-medium text-cyan-300">
                      Ziel
                    </span>
                  )}
                  <span className="flex-1 truncate text-sm text-slate-400">
                    {t.prompt}
                  </span>
                  <span className="text-xs text-slate-500">{formatDate(t.created_at)}</span>
                  <span className="text-slate-600">{expanded === t.id ? "▲" : "▼"}</span>
                </button>

                {expanded === t.id && (
                  <div className="space-y-3 border-t border-slate-800 p-4">
                    <div>
                      <div className="text-xs uppercase tracking-wide text-slate-500">
                        {t.mode === "goal" ? "Ziel" : "Aufgabe"}
                      </div>
                      <p className="mt-1 whitespace-pre-wrap text-sm text-slate-300">{t.prompt}</p>
                    </div>

                    <div className="flex flex-wrap items-center gap-3 text-xs text-slate-500">
                      {t.commit_hash ? (
                        commitUrl(project, t.commit_hash) ? (
                          <a
                            href={commitUrl(project, t.commit_hash)!}
                            target="_blank"
                            rel="noreferrer"
                            className="rounded bg-slate-800 px-2 py-0.5 font-mono text-slate-300 hover:text-cyan-400"
                          >
                            ⎇ {t.commit_hash.slice(0, 8)}
                          </a>
                        ) : (
                          <span className="font-mono">⎇ {t.commit_hash.slice(0, 8)}</span>
                        )
                      ) : (
                        <span>kein Commit</span>
                      )}
                      <span>{t.pushed ? "gepusht ✓" : "nicht gepusht"}</span>
                      {t.exit_code !== null && <span>exit {t.exit_code}</span>}
                    </div>

                    {t.error && (
                      <pre className="overflow-auto rounded-lg border border-red-500/30 bg-red-500/10 p-3 font-mono text-xs whitespace-pre-wrap text-red-300">
                        {t.error}
                      </pre>
                    )}

                    <div>
                      <div className="text-xs uppercase tracking-wide text-slate-500">Ausgabe</div>
                      <pre className="mt-1 max-h-96 overflow-auto rounded-lg bg-slate-950 p-3 font-mono text-xs whitespace-pre-wrap text-slate-300">
                        {outputs[t.id] ?? "Lädt…"}
                      </pre>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Pull output dialog */}
      {pullDialog.open && (
        <Modal
          title={pullDialog.success ? "Pull abgeschlossen" : "Pull fehlgeschlagen"}
          onClose={() => setPullDialog((d) => ({ ...d, open: false }))}
        >
          <pre className={`max-h-64 overflow-auto rounded-lg bg-slate-950 p-3 font-mono text-xs whitespace-pre-wrap ${pullDialog.success ? "text-slate-300" : "text-red-300"}`}>
            {pullDialog.output}
          </pre>
          <div className="mt-4 flex justify-end">
            <Button onClick={() => setPullDialog((d) => ({ ...d, open: false }))}>
              Schließen
            </Button>
          </div>
        </Modal>
      )}
    </div>
  );
}
