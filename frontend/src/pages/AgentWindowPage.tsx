import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import SessionTerminalModal from "../components/SessionTerminalModal";
import TaskConsole from "../components/TaskConsole";
import { ErrorText, Spinner } from "../components/ui";
import type { Agent, Project, Task } from "../types";

/**
 * Standalone page rendered when the user opens a task / goal / session in
 * its own browser tab (popup). It deliberately does NOT include the
 * dashboard header / navigation — the whole point is a focused view that
 * stays open in a separate tab while the user keeps the dashboard around.
 *
 * The route decides what to render:
 *   /windows/task/:taskId    -> one-shot TaskConsole (task + goal share this)
 *   /windows/session/:taskId -> PTY-backed SessionTerminalModal
 *
 * Auth still goes through RequireAuthInline in App.tsx; if the token is
 * missing or the project can't be loaded the page surfaces a friendly
 * error and offers a "Back to dashboard" link.
 */
export default function AgentWindowPage({
  kind,
}: {
  kind: "task" | "session";
}) {
  const { taskId = "" } = useParams();
  const [task, setTask] = useState<Task | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        // For tasks / goals we need the Task (so we can show its prompt as
        // the title and resolve the project). For sessions the same Task
        // object has is_session=true; reusing /tasks/:id for both keeps the
        // page uniform.
        const fetched = await api.getTask(taskId);
        if (!active) return;
        setTask(fetched);
        const [p, ag] = await Promise.all([
          api.getProject(fetched.project_id),
          api.agents(),
        ]);
        if (!active) return;
        setProject(p);
        setAgents(ag);
      } catch (err) {
        if (active) {
          setError(
            err instanceof Error ? err.message : "Agent-Fenster konnte nicht geladen werden",
          );
        }
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [taskId]);

  const agentName = useMemo(() => {
    const m: Record<string, string> = {};
    for (const a of agents) m[a.key] = a.display_name;
    return m;
  }, [agents]);

  const label = useMemo(() => {
    if (!task) return "";
    const who = agentName[task.agent] ?? task.agent;
    if (kind === "session") {
      return `${who} · Session${task.prompt ? ` — ${task.prompt.slice(0, 60)}` : ""}`;
    }
    const modeLabel = task.mode === "goal" ? "Ziel" : "Aufgabe";
    return `${who} · ${modeLabel}${task.prompt ? ` — ${task.prompt.slice(0, 80)}` : ""}`;
  }, [agentName, kind, task]);

  function close() {
    // Closing the popup tab is the natural "I'm done watching" gesture. We
    // do NOT end the agent run here — the backend keeps the task / session
    // alive so the user can reopen the window later from the dashboard.
    window.close();
    // `window.close()` is a no-op when this tab wasn't opened via
    // window.open() / target=_blank. Fall back to navigating back to the
    // dashboard so the user always has an escape hatch.
    setTimeout(() => {
      if (!document.hidden) {
        window.location.href = "#/";
      }
    }, 50);
  }

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center bg-slate-950 text-slate-400">
        <Spinner className="h-6 w-6" />
      </div>
    );
  }
  if (error || !task || !project) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-4 bg-slate-950 p-6 text-center">
        <ErrorText>{error || "Agent-Fenster nicht verfügbar."}</ErrorText>
        <a
          href="#/"
          className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-800"
        >
          ← Zurück zum Dashboard
        </a>
      </div>
    );
  }

  // For sessions the SessionTerminalModal brings its own dark backdrop +
  // title bar with Schließen / Vollbild / Session beenden. We just wire
  // its onClose to close() so a single click on "Schließen" closes the
  // popup tab — fixing the old "double-click to close" behaviour.
  if (kind === "session") {
    return (
      <SessionTerminalModal
        project={project}
        agents={agents}
        taskId={taskId}
        onClose={close}
        onEnded={() => undefined}
      />
    );
  }

  // For tasks / goals we render TaskConsole inside a minimal shell with a
  // single "Schließen" button. The user can use the console's own Vollbild
  // toggle; "Schließen" only closes this popup tab, not the agent run.
  return (
    <div className="flex h-screen flex-col bg-slate-950">
      <div className="flex shrink-0 items-center justify-between gap-3 border-b border-slate-800 bg-slate-900 px-4 py-2">
        <span className="truncate text-sm font-medium text-slate-200">{label}</span>
        <button
          onClick={close}
          className="rounded-lg border border-slate-700 px-2.5 py-1 text-xs text-slate-200 hover:bg-slate-800"
          title="Fenster schließen (läuft im Hintergrund weiter)"
        >
          ✕ Schließen
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        <TaskConsole taskId={taskId} title={label} onDone={() => undefined} />
      </div>
    </div>
  );
}