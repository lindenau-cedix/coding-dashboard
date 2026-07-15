import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, commitUrl } from "../api";
import { subscribe as subscribeCrossTab } from "../crossTab";
import FileBrowser from "../components/FileBrowser";
import TaskConsole from "../components/TaskConsole";
import TaskImages from "../components/TaskImages";
import {
  Button,
  ErrorText,
  FullscreenShell,
  IconButton,
  Modal,
  Spinner,
  StatusBadge,
  formatDate,
} from "../components/ui";
import { useProject } from "../projectContext";
import { openAgentWindow } from "../components/WindowManager";
import type { Agent, EnvProfile, Project, RunningTask, Runner, Task, TaskImagePayload, TaskMode } from "../types";

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

type AgentChoice = {
  value: string;
  agentKey: string;
  runner: Runner;
  label: string;
  disabled: boolean;
};

type RunnerChoice = {
  value: Runner;
  label: string;
};

function baseAgentKey(key: string): string {
  return key.endsWith("-host") ? key.slice(0, -5) : key;
}

function isHostAgentChoice(agent: Agent): boolean {
  return agent.key.endsWith("-host");
}

function supportsMode(agent: Agent, mode: TaskMode): boolean {
  if (mode === "goal") return agent.supports_goal;
  if (mode === "session") return agent.supports_session;
  return true;
}

/** Build the Agent <select> options: one entry per base agent (no doubled
 *  Container/Host entries). When only a hand-written `<base>-host` sibling
 *  exists without its base, expose the host variant directly (mirrors the
 *  previous fallback). */
function buildAgentChoices(agents: Agent[], mode: TaskMode): AgentChoice[] {
  const choices: AgentChoice[] = [];
  const hostKeysRepresented = new Set<string>();
  const baseKeys = new Set<string>();

  for (const agent of agents) {
    if (isHostAgentChoice(agent) || !supportsMode(agent, mode)) continue;
    baseKeys.add(agent.key);
    choices.push({
      value: agent.key,
      agentKey: agent.key,
      runner: "",
      label: agent.display_name,
      disabled: !agent.enabled,
    });
    if (agent.host_agent_key) {
      hostKeysRepresented.add(agent.host_agent_key);
    }
  }

  // Keep explicitly configured host agents usable even when their base entry
  // is absent from a hand-written config. Normal generated configs are folded
  // into the base agent's entry above; only hand-written configs hit this path.
  for (const agent of agents) {
    if (
      !isHostAgentChoice(agent)
      || hostKeysRepresented.has(agent.key)
      || baseKeys.has(baseAgentKey(agent.key))
      || !supportsMode(agent, mode)
    ) {
      continue;
    }
    choices.push({
      value: agent.key,
      agentKey: agent.key,
      runner: "host",
      label: `${agent.display_name} (Host)`,
      disabled: !agent.enabled,
    });
  }

  return choices;
}

/** Runner choices available for the currently selected agent. Hidden in the
 *  UI when the array has a single entry (no host sibling for this agent+mode).
 *  Order: Container first, then Host via SSH. */
function runnerOptions(
  selected: Agent | undefined,
  agents: Agent[],
  mode: TaskMode,
): RunnerChoice[] {
  if (!selected) return [{ value: "", label: "Container" }];
  const host = selected.host_agent_key
    ? agents.find((candidate) => candidate.key === selected.host_agent_key)
    : undefined;
  if (!host || !supportsMode(host, mode) || !host.enabled) {
    return [{ value: "", label: "Container" }];
  }
  return [
    { value: "", label: "Container" },
    { value: "host", label: "Host via SSH" },
  ];
}

export default function ProjectDetail() {
  const { id = "" } = useParams();
  const ctx = useProject();
  const [project, setProject] = useState<Project | null>(ctx.project);
  const [agents, setAgents] = useState<Agent[]>(ctx.agents);
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
  // Per-task execution target selected through the Agent dropdown. Claude Code
  // and Hermes expose explicit container/SSH choices there; the backend still
  // receives the existing base-agent + runner payload for compatibility.
  const [runner, setRunner] = useState<Runner>("");
  const [envProfileKey, setEnvProfileKey] = useState("");
  // Loaded once per visit — the dropdown's items come from here.
  const [profiles, setProfiles] = useState<EnvProfile[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [submitting, setSubmitting] = useState(false);
  // Multiple tasks/goals can run at once (each on its own branch); show a live
  // console for every one that's active.
  const [activeTaskIds, setActiveTaskIds] = useState<string[]>([]);

  const [expanded, setExpanded] = useState<string | null>(null);
  const [outputs, setOutputs] = useState<Record<string, string>>({});
  const [fsOutput, setFsOutput] = useState<{ task: Task; text: string } | null>(null);

  const [showAgentsMd, setShowAgentsMd] = useState(false);
  const [agentsMd, setAgentsMd] = useState<string | null>(null);
  const agentsMdLoaded = useRef(false);
  const [showFiles, setShowFiles] = useState(false);
  const [pulling, setPulling] = useState(false);
  const [pullDialog, setPullDialog] = useState<{ open: boolean; output: string; success: boolean }>({
    open: false,
    output: "",
    success: false,
  });
  const [unarchiving, setUnarchiving] = useState(false);

  const agentName = useMemo(() => {
    const m: Record<string, string> = {};
    for (const a of agents) m[a.key] = a.display_name;
    return m;
  }, [agents]);

  const goalSupported = useMemo(() => agents.some((a) => a.supports_goal), [agents]);
  const sessionSupported = useMemo(() => agents.some((a) => a.supports_session), [agents]);
  const currentAgent = useMemo(
    () => agents.find((a) => a.key === agent) ?? agents.find((a) => a.key === `${agent}-host`),
    [agents, agent],
  );
  const agentChoices = useMemo(() => buildAgentChoices(agents, mode), [agents, mode]);
  // Runner dropdown options for the currently selected agent. When the array
  // has a single "Container" entry, the Runner <select> is hidden entirely
  // (the host option isn't available for this agent+mode).
  const currentRunnerOptions = useMemo(
    () => runnerOptions(currentAgent ?? undefined, agents, mode),
    [currentAgent, agents, mode],
  );
  const showRunnerDropdown = currentRunnerOptions.length > 1;
  const modeOptions = useMemo<TaskMode[]>(() => {
    const options: TaskMode[] = ["task"];
    if (goalSupported) options.push("goal");
    if (sessionSupported) options.push("session");
    return options;
  }, [goalSupported, sessionSupported]);

  function changeAgentChoice(value: string) {
    const choice = agentChoices.find((candidate) => candidate.value === value);
    if (!choice) return;

    setAgent(choice.agentKey);
    // Agent dropdown no longer carries runner info; the user picks Container
    // vs Host via SSH in the dedicated Runner <select> below. Honour the
    // ``runner`` already encoded in the choice for hand-written configs
    // that surface a host-only entry — for everything else, reset to "".
    setRunner(choice.runner);
    // Drop selections the new agent does not offer ("" = agent default).
    const a = agents.find((x) => x.key === choice.agentKey);
    setModel((m) => (a?.model_choices?.includes(m) ? m : ""));
    setEffort((e) => (a?.effort_choices?.includes(e) ? e : ""));
    // Env profiles apply to the Claude family, regardless of whether the
    // selected execution target is its container or SSH sibling.
    setEnvProfileKey(baseAgentKey(choice.agentKey) === "claude" ? envProfileKey : "");
  }

  function changeRunnerChoice(next: Runner) {
    // Force the runner back to Container if the user picks an agent whose
    // host sibling isn't available for this mode.
    const opts = runnerOptions(currentAgent ?? undefined, agents, mode);
    if (!opts.some((o) => o.value === next)) {
      setRunner("");
      return;
    }
    setRunner(next);
  }

  function changeMode(next: TaskMode) {
    setMode(next);
    if (next === "goal") {
      const current = agents.find((a) => a.key === agent);
      if (!current || !current.supports_goal) {
        const first = buildAgentChoices(agents, "goal").find((choice) => !choice.disabled);
        if (first) {
          setAgent(first.agentKey);
          setRunner(first.runner);
        }
      }
    } else if (next === "session") {
      setImages([]);
      const current = agents.find((a) => a.key === agent);
      if (!current || !current.supports_session) {
        const first = buildAgentChoices(agents, "session").find((choice) => !choice.disabled);
        if (first) {
          setAgent(first.agentKey);
          setRunner(first.runner);
        }
      }
      // Sessions DO support the host runner and env-profile overlays
      // (SessionManager.start wires both — the sibling's host_staging
      // flips the workdir to the host-staging copy and the env-overlay
      // is merged onto _build_env before exec). Keeping the previously
      // chosen values when switching into session mode matches the
      // task-mode behaviour; the dropdowns below stay visible.
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
        const [p, ag, ts, profs] = await Promise.all([
          api.getProject(id),
          api.agents(),
          api.listTasks(id),
          api.listEnvProfiles().catch(() => [] as EnvProfile[]),
        ]);
        if (!active) return;
        setProject(p);
        setAgents(ag);
        ctx.setProject(p);
        ctx.setAgents(ag);
        setTasks(ts);
        setProfiles(profs);
        // Pick the default agent from the SAME choice list the dropdown
        // renders, not the raw agent array: buildAgentChoices yields one
        // entry per base agent and a host-only fallback when a hand-written
        // config registers `<base>-host` without its base. Using
        // ``ag.find(a => a.enabled)`` here would default to whatever entry
        // comes first — which can be ``claude-host`` — silently starting an
        // SSH run on the very first submit even though the UI reads
        // "Container". mode is still "task" at initial load.
        const firstChoice = buildAgentChoices(ag, "task").find((c) => !c.disabled);
        if (firstChoice) {
          setAgent(firstChoice.agentKey);
          setRunner(firstChoice.runner);
        }
        const live = ts
          .filter((t) => !t.is_session && (t.status === "running" || t.status === "queued"))
          .map((t) => t.id);
        if (live.length) setActiveTaskIds(live);
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

  // Cross-tab fix: the popup SessionTerminalModal calls
  // ``broadcast({type:"session-done", taskId, status})`` when the user ends
  // the session from there. If the user opened the popup from here, this
  // tab's cached ``tasks`` list still shows the session with ``status=
  // "running"`` — refresh the list immediately so the history row + the
  // git footer update without a manual reload (issue #5).
  useEffect(() => {
    const unsubscribe = subscribeCrossTab((event) => {
      if (event.type === "session-done" || event.type === "task-done") {
        void reloadAgentsMd();
        void refreshTasks();
      }
    });
    return unsubscribe;
  }, [id]);

  // Clamp the runner whenever the selected agent+mode no longer supports
  // the host variant. This catches config reloads (the host sibling
  // disappeared), mode switches (the host sibling doesn't support
  // session/goal), and direct agent changes. Without this guard, the
  // Runner <select> would silently become inconsistent with its parent
  // Agent.
  useEffect(() => {
    if (runner === "") return;
    const opts = runnerOptions(currentAgent ?? undefined, agents, mode);
    if (!opts.some((o) => o.value === runner)) {
      setRunner("");
    }
  }, [currentAgent, agents, mode, runner]);

  /** Resolve the actual agent key for the selected choice. The UI exposes
   *  Container and Host via SSH through separate controls, but the backend
   *  stores the underlying AgentSpec key (e.g. ``claude`` vs ``claude-host``)
   *  so existing tasks can keep filtering on it directly. */
  function resolveSubmitAgentKey(): string {
    if (runner === "host") {
      const base = baseAgentKey(agent);
      const hostKey = `${base}-host`;
      if (agents.some((candidate) => candidate.key === hostKey)) {
        return hostKey;
      }
    }
    return agent;
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!agent || !prompt.trim()) return;
    setSubmitting(true);
    setError("");
    try {
      const submitAgent = resolveSubmitAgentKey();
      const task = await api.createTask(
        id,
        submitAgent,
        prompt.trim(),
        mode,
        model,
        effort,
        images,
        runner,
        envProfileKey,
      );
      setActiveTaskIds((ids) => [task.id, ...ids.filter((x) => x !== task.id)]);
      setPrompt("");
      setImages([]);
      await refreshTasks();
      // Also pin to the floating window so the user can keep watching while
      // they create the next task or navigate elsewhere.
      openAgentWindow(
        toRunningTask(task, project),
        agentName[task.agent] ?? task.agent,
      );
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
      const submitAgent = resolveSubmitAgentKey();
      const { task_id } = await api.createSession(
        id,
        submitAgent,
        "",
        "",
        sessionStartArgs.trim(),
        runner,
        envProfileKey,
      );
      setSessionStartArgs("");
      await refreshTasks();
      // Pin the new session to its own browser tab. Closing the tab does NOT
      // end the session — the user can reopen it from the dashboard's tray
      // tab or from the projects page history.
      const sessTask: Task = {
        id: task_id,
        project_id: id,
        agent: submitAgent,
        prompt: sessionStartArgs.trim(),
        mode: "session",
        model: "",
        effort: "",
        images: [],
        is_session: true,
        chat_history: [],
        status: "running",
        exit_code: null,
        result_summary: "",
        error: "",
        branch: "",
        merge_state: "",
        commit_hash: "",
        commit_message: "",
        commit_created: false,
        pushed: false,
        heartbeat_spawned: false,
        heartbeat_issue_number: null,
        heartbeat_commented_at: null,
        heartbeat_closed_at: null,
        runner,
        env_profile_key: envProfileKey,
        created_at: new Date().toISOString(),
        started_at: null,
        finished_at: null,
      };
      openAgentWindow(
        toRunningTask(sessTask, project),
        agentName[submitAgent] ?? submitAgent,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Session konnte nicht gestartet werden");
    } finally {
      setSubmitting(false);
    }
  }

  /** Cast a Task to the RunningTask shape the WindowManager expects. */
  function toRunningTask(t: Task, p: Project | null): RunningTask {
    return {
      ...t,
      project_name: p?.name ?? "",
      project_slug: p?.slug ?? "",
    };
  }

  async function toggleExpand(task: Task) {
    if (task.is_session || task.mode === "session") {
      // Sessions live in their own browser tab — opening it from history is
      // the same gesture as starting one: a single click pops the dedicated
      // window (or pins to the floating tray if popups are blocked). The
      // session itself keeps running until the user clicks "Session beenden"
      // inside that window.
      openAgentWindow(toRunningTask(task, project), agentName[task.agent] ?? task.agent);
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
        agentsMdLoaded.current = true;
      } catch {
        setAgentsMd("(AGENTS.md konnte nicht geladen werden)");
      }
    }
  }

  /** Re-fetch AGENTS.md after a run so the displayed context stays current.
   *  Only refetches once it has been loaded at least once (panel opened). */
  async function reloadAgentsMd() {
    if (!agentsMdLoaded.current) return;
    try {
      const res = await api.agentsMd(id);
      setAgentsMd(res.exists ? res.content : "(AGENTS.md existiert noch nicht)");
    } catch {
      /* keep previous content */
    }
  }

  function onTaskDone() {
    void refreshTasks();
    void reloadAgentsMd();
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

  /** Pull an archived project back into the active list from the detail page. */
  async function unarchive() {
    setUnarchiving(true);
    try {
      const updated = await api.unarchiveProject(id);
      setProject(updated);
      ctx.setProject(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Wiederherstellen fehlgeschlagen");
    } finally {
      setUnarchiving(false);
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
            {project.archived && (
              <span className="rounded bg-amber-500/15 px-2 py-0.5 text-xs font-medium text-amber-300">
                Archiviert
              </span>
            )}
            <button
              onClick={pull}
              disabled={pulling}
              className="rounded border border-cyan-700 bg-cyan-900/30 px-2.5 py-1 text-xs font-medium text-cyan-400 transition hover:bg-cyan-900/60 disabled:opacity-50"
            >
              {pulling ? "Pulling…" : "Pull"}
            </button>
            {project.archived && (
              <button
                onClick={unarchive}
                disabled={unarchiving}
                title="Aus dem Archiv zurückholen"
                className="rounded border border-cyan-700 bg-cyan-900/30 px-2.5 py-1 text-xs font-medium text-cyan-400 transition hover:bg-cyan-900/60 disabled:opacity-50"
              >
                {unarchiving ? "Hole zurück…" : "↩ Wiederherstellen"}
              </button>
            )}
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
              onChange={(e) => changeAgentChoice(e.target.value)}
              className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-cyan-500"
            >
              {agentChoices.map((choice) => (
                <option key={choice.value} value={choice.value} disabled={choice.disabled}>
                  {choice.label}
                  {choice.disabled ? " (deaktiviert)" : ""}
                </option>
              ))}
            </select>
          </span>
          {showRunnerDropdown && (
            <span className="flex items-center gap-2">
              <label className="text-sm text-slate-400">Runner:</label>
              <select
                value={runner}
                onChange={(e) => changeRunnerChoice(e.target.value as Runner)}
                className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-cyan-500"
                title="Container läuft im Dashboard-Container; Host via SSH führt den Agent auf dem Host aus (geteilter Staging-Ordner)."
              >
                {currentRunnerOptions.map((opt) => (
                  <option key={opt.value || "container"} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </span>
          )}
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
          {/* Env-profile overlay. For today only Claude supports it
              (other agents ignore the field on the server). Available
              in session mode too — SessionManager.start applies the
              overlay onto _build_env before the PTY subprocess execs. */}
          {baseAgentKey(agent) === "claude" && profiles.length > 0 && (
            <span className="flex items-center gap-2">
              <label className="text-sm text-slate-400">Env-Profil:</label>
              <select
                value={envProfileKey}
                onChange={(e) => setEnvProfileKey(e.target.value)}
                className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-cyan-500"
              >
                <option value="">Standard (Anthropic)</option>
                {profiles.map((p) => (
                  <option key={p.key} value={p.key}>
                    {p.name}
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
              placeholder='z.B. --model opus, --resume "session-id" oder bei Codex: resume "session-id"'
              className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-500"
            />
            <span className="mt-1 block text-xs text-slate-500">
              Eine beendete Session kann per Resume-Parameter fortgesetzt werden – sie
              startet automatisch im ursprünglichen Ordner. Parallele Sessions im selben
              Projekt laufen in einer isolierten Arbeitskopie.
            </span>
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

      {/* Live consoles (one per active task/goal — they run in parallel) */}
      {activeTaskIds.length > 0 && (
        <div className="space-y-2">
          <h2 className="font-medium text-slate-200">
            Live-Ausgabe{activeTaskIds.length > 1 ? ` (${activeTaskIds.length})` : ""}
          </h2>
          <div className="space-y-3">
            {activeTaskIds.map((tid) => {
              const t = tasks.find((x) => x.id === tid);
              const label = t
                ? `${agentName[t.agent] ?? t.agent}${t.mode === "goal" ? " · Ziel" : ""} — ${
                    t.prompt ? t.prompt.slice(0, 80) : ""
                  }`
                : undefined;
              return (
                <TaskConsole
                  key={tid}
                  taskId={tid}
                  title={label}
                  onDone={onTaskDone}
                  onDismiss={() =>
                    setActiveTaskIds((ids) => ids.filter((x) => x !== tid))
                  }
                />
              );
            })}
          </div>
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

      {/* File browser */}
      <div className="space-y-2">
        <button
          onClick={() => setShowFiles((v) => !v)}
          className="flex w-full items-center justify-between rounded-2xl border border-slate-800 bg-slate-900 px-5 py-3 text-left text-sm font-medium text-slate-200"
        >
          <span>📂 Dateien durchsuchen</span>
          <span className="text-slate-500">{showFiles ? "▲" : "▼"}</span>
        </button>
        {showFiles && <FileBrowser projectId={id} />}
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
                  {t.merge_state === "conflict" && (
                    <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-xs font-medium text-amber-300">
                      Merge-Konflikt
                    </span>
                  )}
                  <span className="flex-1 truncate text-sm text-slate-400">
                    {t.mode === "session"
                      ? t.prompt
                        ? `Start: ${t.prompt}`
                        : "ohne Startparameter"
                      : t.prompt}
                  </span>
                  {t.heartbeat_spawned && (
                    <span
                      className="shrink-0 rounded bg-cyan-500/15 px-1.5 py-0.5 text-xs font-medium text-cyan-300"
                      title={`Automatisch vom Heartbeat für GitHub-Issue #${t.heartbeat_issue_number ?? "?"} gestartet`}
                    >
                      🤖 Auto-Fix #{t.heartbeat_issue_number ?? "?"}
                    </span>
                  )}
                  {t.runner === "host" && (
                    <span
                      className="shrink-0 rounded bg-emerald-500/15 px-1.5 py-0.5 text-xs font-medium text-emerald-300"
                      title="Auf dem Host per SSH ausgefuehrt (CD_<AGENT>_SSH_USER)"
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
                  {t.heartbeat_commented_at && (
                    <span
                      className="shrink-0 rounded bg-cyan-500/15 px-1.5 py-0.5 text-xs text-cyan-300"
                      title={`Dashboard hat auf GitHub-Issue #${t.heartbeat_issue_number ?? "?"} kommentiert`}
                    >
                      💬 kommentiert
                    </span>
                  )}
                  {t.heartbeat_closed_at && (
                    <span
                      className="shrink-0 rounded bg-emerald-500/15 px-1.5 py-0.5 text-xs text-emerald-300"
                      title={`Dashboard hat GitHub-Issue #${t.heartbeat_issue_number ?? "?"} geschlossen`}
                    >
                      ✓ geschlossen
                    </span>
                  )}
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
                      {t.merge_state === "merged" && (
                        <span className="text-emerald-400">gemerged → {project.default_branch}</span>
                      )}
                      {t.merge_state === "conflict" && (
                        <span className="text-amber-400" title="Branch blieb erhalten für manuellen Merge">
                          Merge-Konflikt · Branch {t.branch}
                        </span>
                      )}
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
                      <div className="flex items-center justify-between">
                        <div className="text-xs uppercase tracking-wide text-slate-500">
                          Ausgabe
                        </div>
                        {outputs[t.id] !== undefined && (
                          <IconButton
                            label="Vollbild"
                            onClick={() =>
                              setFsOutput({ task: t, text: outputs[t.id] ?? "" })
                            }
                          >
                            ⛶
                          </IconButton>
                        )}
                      </div>
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
      {fsOutput && (
        <FullscreenShell
          title={
            <span className="flex items-center gap-2">
              <StatusBadge status={fsOutput.task.status} />
              {agentName[fsOutput.task.agent] ?? fsOutput.task.agent}
              <span className="truncate text-slate-400">{fsOutput.task.prompt}</span>
            </span>
          }
          onClose={() => setFsOutput(null)}
        >
          <pre className="min-h-0 flex-1 overflow-auto rounded-lg bg-slate-950 p-4 font-mono text-sm leading-relaxed whitespace-pre-wrap text-slate-300">
            {fsOutput.text || "(keine Ausgabe)"}
          </pre>
        </FullscreenShell>
      )}
    </div>
  );
}
