import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { Agent, Project, RunningTask, TaskMode } from "../types";
import SessionTerminalModal from "./SessionTerminalModal";
import TaskConsole from "./TaskConsole";
import { IconButton, Spinner, StatusBadge } from "./ui";

/** A single floating window. Either a one-shot task/goal console or a
 *  session terminal. Multiple may be open at the same time. */
export interface OpenWindow {
  taskId: string;
  /** Whether the underlying task is a session (PTY-backed TUI). Decides
   *  which renderer to mount inside the window. */
  isSession: boolean;
  /** Snapshot taken at open-time so the tab title stays meaningful even when
   *  the live `/running` poll is stale or the task has finished. */
  title: string;
  projectId: string;
  projectName: string;
  agentLabel: string;
  status: RunningTask["status"];
  mode: TaskMode;
  /** Persisted across reloads so a refresh doesn't lose the user's setup. */
  pinned: boolean;
}

const STORAGE_KEY = "cd_open_windows_v1";

function loadPersisted(): OpenWindow[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (w): w is OpenWindow =>
        w &&
        typeof w.taskId === "string" &&
        typeof w.projectId === "string" &&
        typeof w.title === "string",
    );
  } catch {
    return [];
  }
}

function savePersisted(windows: OpenWindow[]) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(windows));
  } catch {
    /* quota / private mode — ignore */
  }
}

/** Resolve the agent route that hosts this task in its own browser tab. */
function agentWindowUrl(task: RunningTask): string {
  const isSession = task.is_session || task.mode === "session";
  const kind = isSession ? "session" : "task";
  return `#/windows/${kind}/${task.id}`;
}

/** Open an agent run in its own browser tab. The window keeps streaming
 *  output / PTY bytes even when the user closes the popup — the backend
 *  task / session itself is unaffected, only the tab goes away.
 *
 *  Returns the opened Window reference (or null when the popup was
 *  blocked, so the caller can decide whether to fall back to the in-tab
 *  floating window). */
export function openAgentWindowInNewTab(task: RunningTask): Window | null {
  if (typeof window === "undefined") return null;
  const url = `${window.location.origin}${window.location.pathname}${agentWindowUrl(task)}`;
  // Reasonable default size for the popup; the user can resize freely. We
  // intentionally don't pass `noopener` — the popup shares origin / storage
  // so it can read the auth token + open WebSocket connections.
  const features = "width=1100,height=820,resizable=yes,scrollbars=no";
  return window.open(url, `agent-${task.id}`, features);
}

/** Imperative bus: components dispatch a CustomEvent when they want to
 *  pin a console for a running task to the floating tray (used when the
 *  popup was blocked or the user explicitly wants the in-tab dock). */
export function pinAgentWindow(task: RunningTask, agentLabel: string): void {
  if (typeof window === "undefined") return;
  const detail: Omit<OpenWindow, "pinned"> = {
    taskId: task.id,
    isSession: task.is_session || task.mode === "session",
    title: task.prompt
      ? task.prompt.slice(0, 60)
      : task.is_session
        ? "interaktive Session"
        : "Aufgabe",
    projectId: task.project_id,
    projectName: task.project_name || task.project_slug || "",
    agentLabel,
    status: task.status,
    mode: task.mode,
  };
  window.dispatchEvent(new CustomEvent("cd-open-window", { detail }));
}

/** Default user-facing entry point: try the popup first; fall back to the
 *  in-tab tray if popups are blocked. This is the function UI handlers
 *  (RunningAgents click, history click, "Session starten") should call. */
export function openAgentWindow(task: RunningTask, agentLabel: string): void {
  const popup = openAgentWindowInNewTab(task);
  if (!popup) {
    // Popup blocked — fall back to the floating window so the user still
    // sees something rather than a silent nothing.
    pinAgentWindow(task, agentLabel);
  } else {
    // Keep the tray tab in sync too: if the user later closes the popup and
    // comes back to the dashboard, the tray tab lets them reopen it without
    // a second network round-trip to /running. Stays open and pinned
    // (popup close ≠ backend stop).
    pinAgentWindow(task, agentLabel);
  }
}

/** Bottom-right floating tray + the currently focused window. Click a tab
 *  to focus a window; "×" to close it (and remove from persisted state). */
export default function WindowManager({
  agents,
  currentProject,
}: {
  agents: Agent[];
  currentProject: Project | null;
}) {
  const [windows, setWindows] = useState<OpenWindow[]>(loadPersisted);
  const [focusId, setFocusId] = useState<string | null>(null);
  const [minimized, setMinimized] = useState(false);
  const [agentNames, setAgentNames] = useState<Record<string, string>>({});
  const initial = useRef(true);

  // Persist windows (skip the very first render so we don't immediately
  // overwrite storage with an empty array on a fresh load).
  useEffect(() => {
    if (initial.current) {
      initial.current = false;
      return;
    }
    savePersisted(windows);
  }, [windows]);

  // Listen for global "open this" events.
  useEffect(() => {
    function onOpen(e: Event) {
      const detail = (e as CustomEvent).detail as Omit<OpenWindow, "pinned">;
      setWindows((prev) => {
        const existing = prev.find((w) => w.taskId === detail.taskId);
        if (existing) return prev;
        return [...prev, { ...detail, pinned: true }];
      });
      setFocusId(detail.taskId);
      setMinimized(false);
    }
    window.addEventListener("cd-open-window", onOpen);
    return () => window.removeEventListener("cd-open-window", onOpen);
  }, []);

  // Refresh agent display labels (cosmetic; cheap).
  useEffect(() => {
    setAgentNames(Object.fromEntries(agents.map((a) => [a.key, a.display_name])));
  }, [agents]);

  // For tasks opened from a project page, drop their entry from the tray once
  // they finish so the tray doesn't grow forever.
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setInterval> | null = null;

    async function poll() {
      try {
        const running = await api.listRunning();
        if (!active) return;
        const alive = new Set(running.map((t) => t.id));
        setWindows((prev) => {
          let changed = false;
          const next = prev.filter((w) => {
            // Drop finished tasks after a short grace window (give the user
            // a chance to inspect the result first).
            if (!alive.has(w.taskId)) {
              if (w.status === "running" || w.status === "queued") {
                // Mark as finished; keep for one more poll cycle, then drop.
                changed = true;
                return { ...w, status: "success" };
              }
              if (w.status === "success" || w.status === "failed" || w.status === "error") {
                // Already marked finished on a previous pass — drop now.
                changed = true;
                return false;
              }
              return true;
            }
            // Still alive: refresh status from the live record.
            const r = running.find((x) => x.id === w.taskId);
            if (r && r.status !== w.status) {
              changed = true;
              return { ...w, status: r.status };
            }
            return true;
          });
          return changed ? next : prev;
        });
      } catch {
        /* polling is best-effort */
      }
    }
    void poll();
    timer = setInterval(poll, 4000);
    return () => {
      active = false;
      if (timer) clearInterval(timer);
    };
  }, []);

  const focusWindow = useMemo(
    () => windows.find((w) => w.taskId === focusId) ?? null,
    [windows, focusId],
  );

  const closeWindow = useCallback((taskId: string) => {
    setWindows((prev) => prev.filter((w) => w.taskId !== taskId));
    setFocusId((prev) => (prev === taskId ? null : prev));
  }, []);

  // Resolve the Project object a window needs to render (SessionTerminal
  // requires it). When we don't have one handy (the user opened from the
  // projects page without navigating into the project), we lazy-fetch it.
  const [resolvedProjects, setResolvedProjects] = useState<Record<string, Project>>({});

  useEffect(() => {
    let cancelled = false;
    const need = new Set<string>();
    for (const w of windows) {
      if (w.isSession && !resolvedProjects[w.projectId] && currentProject?.id !== w.projectId) {
        need.add(w.projectId);
      }
    }
    if (need.size === 0) return;
    (async () => {
      for (const id of Array.from(need)) {
        try {
          const p = await api.getProject(id);
          if (cancelled) return;
          setResolvedProjects((prev) => ({ ...prev, [id]: p }));
        } catch {
          /* skip — window will show a tiny error state */
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [windows, resolvedProjects, currentProject]);

  if (windows.length === 0) return null;

  const focused = focusWindow;
  const projectForWindow = focused
    ? currentProject && currentProject.id === focused.projectId
      ? currentProject
      : resolvedProjects[focused.projectId] || null
    : null;

  // Single-click close: the user clicked "Schließen" inside the session
  // modal (or the tray tab's ×). Either way we drop the window entirely.
  // Minimize (─) is the separate, explicit way to keep the tray tab but
  // hide the body.
  function closeFocused() {
    if (focused) closeWindow(focused.taskId);
  }

  return (
    <>
      {/* Focused window (one at a time, like a tabbed dock). */}
      {focused && !minimized && (
        <div
          className="fixed bottom-14 right-4 z-40 flex max-h-[80vh] w-[min(1100px,calc(100vw-2rem))] flex-col overflow-hidden rounded-2xl border border-slate-700 bg-slate-950 shadow-2xl"
          role="dialog"
          aria-label={`Agent-Fenster ${focused.title}`}
        >
          <div className="flex items-center justify-between gap-2 border-b border-slate-800 bg-slate-900 px-4 py-2">
            <div className="flex min-w-0 items-center gap-2">
              <StatusBadge status={focused.status} />
              <span className="truncate text-sm font-medium text-slate-100">
                {agentNames[focused.agentLabel] || focused.agentLabel}
              </span>
              <span className="truncate text-xs text-slate-500">
                · {focused.projectName}
              </span>
              <span className="min-w-0 flex-1 truncate text-sm text-slate-300">
                · {focused.title}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <IconButton
                label="In eigenem Fenster öffnen"
                onClick={() => {
                  const task: RunningTask = {
                    id: focused.taskId,
                    project_id: focused.projectId,
                    agent: focused.agentLabel,
                    prompt: focused.title,
                    mode: focused.mode,
                    model: "",
                    effort: "",
                    images: [],
                    is_session: focused.isSession,
                    chat_history: [],
                    status: focused.status,
                    exit_code: null,
                    result_summary: "",
                    error: "",
                    branch: "",
                    merge_state: "",
                    commit_hash: "",
                    commit_message: "",
                    commit_created: false,
                    pushed: false,
                    created_at: new Date().toISOString(),
                    started_at: null,
                    finished_at: null,
                    project_name: focused.projectName,
                    project_slug: focused.projectName,
                  };
                  openAgentWindowInNewTab(task);
                }}
              >
                ⧉
              </IconButton>
              <IconButton label="Minimieren" onClick={() => setMinimized(true)}>
                ─
              </IconButton>
              <IconButton label="Schließen" onClick={closeFocused}>
                ✕
              </IconButton>
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-auto">
            {focused.isSession ? (
              projectForWindow ? (
                <SessionTerminalModal
                  project={projectForWindow}
                  agents={agents}
                  taskId={focused.taskId}
                  onClose={closeFocused}
                  onEnded={() => {
                    /* session ended: re-poll will mark it finished and drop it */
                  }}
                />
              ) : (
                <div className="flex items-center gap-2 p-4 text-sm text-slate-400">
                  <Spinner className="h-4 w-4" /> Lade Projektkontext…
                </div>
              )
            ) : (
              <div className="p-3">
                <TaskConsole
                  taskId={focused.taskId}
                  title={`${agentNames[focused.agentLabel] || focused.agentLabel} — ${focused.title}`}
                  onDismiss={() => closeWindow(focused.taskId)}
                />
              </div>
            )}
          </div>
        </div>
      )}

      {/* Bottom tab strip — one tab per open window, click to focus. */}
      <div className="fixed bottom-2 left-1/2 z-40 flex max-w-[calc(100vw-2rem)] -translate-x-1/2 items-center gap-1 rounded-xl border border-slate-700 bg-slate-900/95 px-1 py-1 shadow-lg backdrop-blur">
        {windows.map((w) => {
          const active = w.taskId === focusId;
          return (
            <button
              key={w.taskId}
              onClick={() => {
                setFocusId(w.taskId);
                setMinimized(false);
              }}
              className={`group flex max-w-[280px] items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs transition ${
                active
                  ? "bg-cyan-600 text-white"
                  : "bg-slate-800 text-slate-300 hover:bg-slate-700"
              }`}
              title={`${agentNames[w.agentLabel] || w.agentLabel} · ${w.projectName} · ${w.title}`}
            >
              <StatusBadge status={w.status} />
              <span className="truncate font-medium">
                {agentNames[w.agentLabel] || w.agentLabel}
              </span>
              <span className="hidden truncate text-slate-400 sm:inline">·</span>
              <span className="hidden truncate text-slate-400 sm:inline">{w.projectName}</span>
              <span
                onClick={(e) => {
                  e.stopPropagation();
                  // Single click closes the tray tab (and the window it
                  // represents). No double-click dance: the previous behaviour
                  // of "Schließen" minimising while the tray × actually
                  // removed the window confused users.
                  closeWindow(w.taskId);
                }}
                role="button"
                tabIndex={-1}
                className={`ml-1 flex h-4 w-4 cursor-pointer items-center justify-center rounded-full text-[10px] ${
                  active ? "hover:bg-cyan-700" : "hover:bg-slate-600"
                }`}
                aria-label="Schließen"
                title="Schließen"
              >
                ✕
              </span>
            </button>
          );
        })}
        {windows.length > 0 && (
          <button
            onClick={() => setMinimized((m) => !m)}
            className="ml-1 rounded-lg px-2 py-1 text-xs text-slate-400 hover:bg-slate-800 hover:text-slate-200"
            title={minimized ? "Wiederherstellen" : "Alle minimieren"}
          >
            {minimized ? "▲" : "▼"}
          </button>
        )}
      </div>
    </>
  );
}