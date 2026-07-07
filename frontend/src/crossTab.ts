/**
 * Cross-tab notification helper for the dashboard.
 *
 * Different tabs on the same origin often show overlapping state (e.g. a
 * running agent visible both in the session popup and in the cross-project
 * "Laufende Agenten" list, or in a project detail page's history). When the
 * popup ends the session / task, the popup MUST notify the other tabs so
 * they refresh their cached state immediately instead of waiting for the
 * 3-second poll to catch up (or until the user manually refreshes the page).
 *
 * The helper:
 * - exposes `broadcast(event)` which posts a typed event to a single named
 *   ``BroadcastChannel`` (browser-native, cross-tab, cross-window, no
 *   backend round-trip needed);
 * - exposes `subscribe(handler)` which returns the unsubscribe function.
 *   Each subscriber registers on its own ``BroadcastChannel`` (browser
 *   fan-out) and discards messages it sent itself.
 *
 * Graceful degradation: ``BroadcastChannel`` is unavailable in some
 * environments (SSR, very old browsers). The helper silently no-ops in
 * that case so other code paths still work.
 */
export type CrossTabEvent =
  | { type: "task-done"; taskId: string; status: string }
  | { type: "session-done"; taskId: string; status: string };

const CHANNEL_NAME = "coding-dashboard-status";

function channel(): BroadcastChannel | null {
  if (typeof window === "undefined") return null;
  // Some browsers expose the constructor but the call throws (e.g. when
  // the document hasn't been fully loaded yet). Wrap defensively.
  try {
    return new BroadcastChannel(CHANNEL_NAME);
  } catch {
    return null;
  }
}

export function broadcast(event: CrossTabEvent): void {
  const ch = channel();
  if (!ch) return;
  try {
    ch.postMessage(event);
  } catch {
    /* Best-effort — if the channel dies the polling fallback still works. */
  } finally {
    ch.close();
  }
}

export function subscribe(handler: (event: CrossTabEvent) => void): () => void {
  const ch = channel();
  if (!ch) return () => undefined;
  const listener = (ev: MessageEvent<CrossTabEvent>) => {
    if (ev.data && typeof ev.data === "object" && "type" in ev.data) {
      handler(ev.data);
    }
  };
  ch.addEventListener("message", listener);
  return () => {
    ch.removeEventListener("message", listener);
    ch.close();
  };
}
