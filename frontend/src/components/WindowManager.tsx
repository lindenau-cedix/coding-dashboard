import type { RunningTask } from "../types";

/** Resolve the agent route that hosts this task in its own browser tab. */
function agentWindowUrl(task: RunningTask): string {
  const isSession = task.is_session || task.mode === "session";
  const kind = isSession ? "session" : "task";
  return `#/windows/${kind}/${task.id}`;
}

/** Open an agent run in its own browser tab (popup). The window keeps
 *  streaming output / PTY bytes even when the user closes the popup — the
 *  backend task / session itself is unaffected, only the tab goes away.
 *
 *  Returns the opened Window reference (or null when the popup was
 *  blocked, so the caller can decide what to do). */
export function openAgentWindowInNewTab(task: RunningTask): Window | null {
  if (typeof window === "undefined") return null;
  const url = `${window.location.origin}${window.location.pathname}${agentWindowUrl(task)}`;
  // Reasonable default size for the popup; the user can resize freely. We
  // intentionally don't pass `noopener` — the popup shares origin / storage
  // so it can read the auth token + open WebSocket connections.
  const features = "width=1100,height=820,resizable=yes,scrollbars=no";
  return window.open(url, `agent-${task.id}`, features);
}

/** Default user-facing entry point: open the agent's own browser tab.
 *  If the popup is blocked by the browser, this is a no-op (the browser
 *  will surface a "pop-up blocked" indicator to the user). The
 *  /windows/{task,session}/:id route is the single source of truth for
 *  agent UI — there is no in-tab tray fallback. */
export function openAgentWindow(task: RunningTask, _agentLabel: string): void {
  openAgentWindowInNewTab(task);
}
