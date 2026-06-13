import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, commitUrl } from "../api";
import SessionTerminalModal from "../components/SessionTerminalModal";
import TaskConsole from "../components/TaskConsole";
import TaskImages from "../components/TaskImages";
import { Button, ErrorText, Modal, Spinner, StatusBadge, formatDate } from "../components/ui";
import type { Agent, Project, Task, TaskImagePayload, TaskMode } from "../types";

const MAX_IMAGES = 6;
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

export default function ProjectDetail() {
  const { id = "" } = useParams();
  const [project, setProject] = useState<Project | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const [agent, setAgent] = useState("");
  const [mode, setMode] = useState<TaskMode>("task");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("");
  const [prompt, setPrompt] = useState("");
  const [images, setImages] = useState<TaskImagePayload[]>([]);
  const [sessionStartArgs, setSessionStartArgs] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [submitting, setSubmitting] = useState(false);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [sessionDialogTaskId, setSessionDialogTaskId] = useState<string | null>(null);

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
  const sessionSupported = useMemo(() => agents.some((a) => a.supports_session), [agents]);
  const currentAgent = useMemo(() => agents.find((a) => a.key === agent), [agents, agent]);
  const modeOptions = useMemo<TaskMode[]>(() => {
    const options: TaskMode[] = ["task"];
    if (goalSupported) options.push("goal");
    if (sessionSupported) options.push("session");
    return options;
  }, [goalSupported, sessionSupported]);

  function changeAgent(next: string) {
    setAgent(next);
    // Drop selections the new agent does not offer ("" = agent default).
    const a = agents.find((x) => x.key === next);
    setModel((m) => (a?.model_choices?.includes(m) ? m : ""));
    setEffort((e) => (a?.effort_choices?.includes(e) ? e : ""));
  }
  // In goal mode only agents that support it are selectable.
  const selectableAgents = useMemo(
    () => {
      if (mode === "goal") return agents.filter((a) => a.supports_goal);
      if (mode === "session") return agents.filter((a) => a.supports_session);
      return agents;
    },
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
    } else if (next === "session") {
      setImages([]);
      const current = agents.find((a) => a.key === agent);
      if (!current || !current.supports_session) {
        const first = agents.find((a) => a.supports_session && a.enabled)
          ?? agents.find((a) => a.supports_session);
        if (first) setAgent(first.key);
      }
    }
  }

  async function refreshTasks() {
    setTasks(await api.listTasks(id));
  }

  async function addImageFiles(files: Iterable<File>) {
    setError("");
    const next = [...images];
    for (const file of files) {
      if (!file.type.startsWith("image/")) continue;
      if (next.length >= MAX_IMAGES) {
        setError(`Maximal ${MAX_IMAGES} Bilder pro Aufgabe.`);
        break;
      }
      if (file.size > MAX_IMAGE_BYTES) {
        setError(`"${file.name}" ist größer als ${MAX_IMAGE_BYTES / 1024 / 1024} MB.`);
        continue;
      }
      try {
        const data = await readAsDataUrl(file);
        next.push({ name: file.name || `bild-${next.length + 1}.png`, data });
      } catch {
        setError(`"${file.name}" konnte nicht gelesen werden.`);
      }
    }
    setImages(next);
  }

  function onPaste(e: React.ClipboardEvent) {
    const files = Array.from(e.clipboardData?.items ?? [])
      .filter((it) => it.kind === "file" && it.type.startsWith("image/"))
      .map((it) => it.getAsFile())
      .filter((f): f is File => f !== null);
    if (files.length) {
      e.preventDefault();
      void addImageFiles(files);
    }
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
        const live = ts.find(
          (t) => !t.is_session && (t.status === "running" || t.status === "queued"),
        );
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
      const task = await api.createTask(id, agent, prompt.trim(), mode, model, effort, images);
      setActiveTaskId(task.id);
      setPrompt("");
      setImages([]);
      await refreshTasks();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Task konnte nicht gestartet werden");
    } finally {
      setSubmitting(false);
    }
  }

  async function startSession() {
    if (!agent) return;
    setSubmitting(true);
    setError("");
    try {
      const { task_id } = await api.createSession(id, agent, "", "", sessionStartArgs.trim());
      setSessionDialogTaskId(task_id);
      setSessionStartArgs("");
      await refreshTasks();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Session konnte nicht gestartet werden");
    } finally {
      setSubmitting(false);
    }
  }

  async function toggleExpand(task: Task) {
    if (task.is_session || task.mode === "session") {
      setSessionDialogTaskId(task.id);
      return;
    }
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
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-sm text-slate-400">Modus:</label>
          <div className="inline-flex overflow-hidden rounded-lg border border-slate-700">
            {modeOptions.map((m) => (
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
                {m === "goal" ? "Ziel" : m === "session" ? "Session" : "Aufgabe"}
              </button>
            ))}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <span className="flex items-center gap-2">
            <label className="text-sm text-slate-400">Agent:</label>
            <select
              value={agent}
              onChange={(e) => changeAgent(e.target.value)}
              className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-cyan-500"
            >
              {selectableAgents.map((a) => (
                <option key={a.key} value={a.key} disabled={!a.enabled}>
                  {a.display_name}
                  {a.enabled ? "" : " (deaktiviert)"}
                </option>
              ))}
            </select>
          </span>
          {mode !== "session" && (currentAgent?.model_choices?.length ?? 0) > 0 && (
            <span className="flex items-center gap-2">
              <label className="text-sm text-slate-400">Modell:</label>
              <select
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-cyan-500"
              >
                <option value="">Standard</option>
                {currentAgent!.model_choices.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </span>
          )}
          {mode !== "session" && (currentAgent?.effort_choices?.length ?? 0) > 0 && (
            <span className="flex items-center gap-2">
              <label className="text-sm text-slate-400">Effort:</label>
              <select
                value={effort}
                onChange={(e) => setEffort(e.target.value)}
                className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-cyan-500"
              >
                <option value="">Standard</option>
                {currentAgent!.effort_choices.map((ef) => (
                  <option key={ef} value={ef}>
                    {ef}
                  </option>
                ))}
              </select>
            </span>
          )}
        </div>
        {mode === "session" ? (
          <label className="block">
            <span className="mb-1 block text-sm text-slate-400">Startparameter</span>
            <input
              value={sessionStartArgs}
              onChange={(e) => setSessionStartArgs(e.target.value)}
              placeholder='z.B. --model opus oder --resume "session-id"'
              className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-500"
            />
          </label>
        ) : (
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onPaste={onPaste}
            rows={4}
            placeholder={
              mode === "goal"
                ? "Beschreibe das Ziel – der Agent arbeitet im /goal-Modus, bis es erreicht ist…"
                : "Beschreibe die Aufgabe, die der Agent im Projekt erledigen soll…"
            }
            className="w-full resize-y rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-500"
          />
        )}
        {mode !== "session" && images.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {images.map((img, i) => (
              <div key={`${img.name}-${i}`} className="relative">
                <img
                  src={img.data}
                  alt={img.name}
                  title={img.name}
                  className="h-20 w-20 rounded-lg border border-slate-700 object-cover"
                />
                <button
                  type="button"
                  onClick={() => setImages((arr) => arr.filter((_, j) => j !== i))}
                  title="Bild entfernen"
                  className="absolute -right-2 -top-2 flex h-5 w-5 items-center justify-center rounded-full border border-slate-600 bg-slate-900 text-xs text-slate-300 hover:border-red-500 hover:text-red-400"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
        <div className="flex flex-wrap items-center justify-between gap-2">
          {mode !== "session" ? (
            <div className="flex items-center gap-3">
              <input
                ref={fileInputRef}
                type="file"
                accept="image/png,image/jpeg,image/gif,image/webp"
                multiple
                className="hidden"
                onChange={(e) => {
                  if (e.target.files) void addImageFiles(Array.from(e.target.files));
                  e.target.value = "";
                }}
              />
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={images.length >= MAX_IMAGES}
                className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-300 transition hover:bg-slate-700 disabled:opacity-50"
              >
                📎 Bilder anhängen
              </button>
              <span className="text-xs text-slate-500">
                {images.length > 0
                  ? `${images.length}/${MAX_IMAGES} Bild(er)`
                  : "oder Bild in das Textfeld einfügen (Strg+V)"}
              </span>
            </div>
          ) : (
            <span />
          )}
          <div className="flex items-center gap-3">
            <p className="hidden text-xs text-slate-500 sm:block">
              {mode === "session"
                ? "Interaktive TUI-Session. Nach dem Beenden werden Änderungen automatisch committet & gepusht."
                : mode === "goal"
                  ? "Der gesamte Verlauf bis zum Ziel zählt als ein Task. Änderungen werden danach automatisch committet & gepusht."
                  : "Nach Abschluss werden Änderungen automatisch committet & gepusht."}
            </p>
            {mode === "session" ? (
              <Button
                type="button"
                onClick={() => void startSession()}
                disabled={submitting || !agent || !currentAgent?.supports_session}
              >
                {submitting ? "Startet…" : "Session starten"}
              </Button>
            ) : (
              <Button type="submit" disabled={submitting || !agent || !prompt.trim()}>
                {submitting
                  ? "Startet…"
                  : mode === "goal"
                    ? "Ziel starten"
                    : "Aufgabe starten"}
              </Button>
            )}
          </div>
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
          <span>AGENTS.md (gemeinsamer Kontext für Agenten)</span>
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
                  {t.mode === "session" && (
                    <span className="rounded bg-purple-500/15 px-1.5 py-0.5 text-xs font-medium text-purple-300">
                      Session
                    </span>
                  )}
                  <span className="flex-1 truncate text-sm text-slate-400">
                    {t.mode === "session"
                      ? t.prompt
                        ? `Start: ${t.prompt}`
                        : "ohne Startparameter"
                      : t.prompt}
                  </span>
                  {(t.images?.length ?? 0) > 0 && (
                    <span className="text-xs text-slate-500">📎 {t.images.length}</span>
                  )}
                  <span className="text-xs text-slate-500">{formatDate(t.created_at)}</span>
                  {t.commit_hash ? (
                    <span className="flex items-center gap-1 text-xs text-slate-500">
                      {commitUrl(project, t.commit_hash) ? (
                        <a
                          href={commitUrl(project, t.commit_hash)!}
                          target="_blank"
                          rel="noreferrer"
                          className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-slate-300 hover:text-cyan-400"
                          title={t.commit_hash}
                        >
                          ⎇ {t.commit_hash.slice(0, 8)}
                        </a>
                      ) : (
                        <span className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-slate-300">
                          ⎇ {t.commit_hash.slice(0, 8)}
                        </span>
                      )}
                      <span
                        className={t.pushed ? "text-emerald-400" : "text-amber-400"}
                        title={t.pushed ? "Erfolgreich gepusht" : "Nicht gepusht"}
                      >
                        {t.pushed ? "gepusht ✓" : "nicht gepusht"}
                      </span>
                    </span>
                  ) : t.status === "running" || t.status === "queued" ? (
                    <span className="text-xs text-slate-600">—</span>
                  ) : null}
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

                    {(t.images?.length ?? 0) > 0 && (
                      <div>
                        <div className="text-xs uppercase tracking-wide text-slate-500">
                          Bilder ({t.images.length})
                        </div>
                        <div className="mt-1">
                          <TaskImages taskId={t.id} names={t.images} />
                        </div>
                      </div>
                    )}

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
                      {t.model && (
                        <span className="rounded bg-slate-800 px-2 py-0.5 text-slate-300">
                          {t.model}
                        </span>
                      )}
                      {t.effort && (
                        <span className="rounded bg-slate-800 px-2 py-0.5 text-slate-300">
                          Effort: {t.effort}
                        </span>
                      )}
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
      {sessionDialogTaskId && (
        <SessionTerminalModal
          project={project}
          agents={agents}
          taskId={sessionDialogTaskId}
          onClose={() => setSessionDialogTaskId(null)}
          onEnded={() => {
            void refreshTasks();
          }}
        />
      )}
    </div>
  );
}
