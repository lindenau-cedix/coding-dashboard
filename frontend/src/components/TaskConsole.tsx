import { useEffect, useRef, useState } from "react";
import { api, ensureCloudflareAccess, getToken, wsUrl } from "../api";
import { broadcast } from "../crossTab";
import type { Task, TaskStatus, WsMessage } from "../types";
import { Button, FullscreenShell, IconButton, StatusBadge } from "./ui";

export default function TaskConsole({
  taskId,
  title,
  onDone,
  onDismiss,
}: {
  taskId: string;
  title?: string;
  onDone?: (task: Task) => void;
  onDismiss?: () => void;
}) {
  const [lines, setLines] = useState("");
  const [status, setStatus] = useState<TaskStatus>("queued");
  const [fullscreen, setFullscreen] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const fsScrollRef = useRef<HTMLDivElement>(null);
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;

  useEffect(() => {
    setLines("");
    setStatus("queued");
    let ws: WebSocket | null = null;
    let manuallyClosed = false;

    void (async () => {
      try {
        await ensureCloudflareAccess();
      } catch (err) {
        setLines((prev) =>
          prev +
          `\n[Fehler] ${err instanceof Error ? err.message : "Cloudflare Access fehlgeschlagen"}\n`,
        );
        return;
      }

      if (manuallyClosed) return;
      const token = getToken() ?? "";
      ws = new WebSocket(
        wsUrl(`/api/ws/tasks/${taskId}?token=${encodeURIComponent(token)}`),
      );

      ws.onmessage = (ev) => {
        let msg: WsMessage;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.type === "output" || msg.type === "git") {
          const data = (msg as { data: string }).data;
          setLines((prev) => prev + data);
        } else if (msg.type === "status") {
          setStatus((msg as { status: TaskStatus }).status);
        } else if (msg.type === "done") {
          const task = (msg as { task: Task }).task;
          setStatus(task.status);
          // Cross-tab notification: a sibling tab's "Laufende Agenten" panel
          // (or a project-history view) needs to drop this task from its
          // /running view immediately, not on the next 3s poll. See #5.
          broadcast({ type: "task-done", taskId, status: task.status });
          onDoneRef.current?.(task);
        } else if (msg.type === "error") {
          const m = (msg as { message: string }).message;
          setLines((prev) => prev + `\n[Fehler] ${m}\n`);
        }
      };
      ws.onerror = () => {
        setLines((prev) => prev + "\n[Fehler] WebSocket-Verbindung fehlgeschlagen.\n");
      };
    })();

    return () => {
      manuallyClosed = true;
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        ws.close();
      }
    };
  }, [taskId]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    const fsEl = fsScrollRef.current;
    if (fsEl) fsEl.scrollTop = fsEl.scrollHeight;
  }, [lines, fullscreen]);

  const running = status === "running" || status === "queued";

  async function stop() {
    try {
      await api.stopTask(taskId);
    } catch {
      /* ignore */
    }
  }

  const controls = (
    <>
      {running && (
        <Button variant="danger" onClick={stop} className="px-2.5 py-1 text-xs">
          Stop
        </Button>
      )}
      <IconButton
        label={fullscreen ? "Vollbild verlassen" : "Vollbild"}
        onClick={() => setFullscreen((v) => !v)}
      >
        {fullscreen ? "🗗" : "⛶"}
      </IconButton>
      {!running && onDismiss && (
        <IconButton label="Ausblenden" onClick={onDismiss}>
          ✕
        </IconButton>
      )}
    </>
  );

  const body = lines || <span className="text-slate-600">Warte auf Ausgabe…</span>;

  return (
    <>
      <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-950">
        <div className="flex items-center justify-between gap-2 border-b border-slate-800 px-3 py-2">
          <div className="flex min-w-0 items-center gap-2">
            <StatusBadge status={status} />
            {title && <span className="truncate text-xs text-slate-400">{title}</span>}
          </div>
          <div className="flex items-center gap-2">{controls}</div>
        </div>
        <div
          ref={scrollRef}
          className="max-h-96 min-h-24 overflow-y-auto whitespace-pre-wrap break-words p-3 font-mono text-xs leading-relaxed text-slate-200"
        >
          {body}
        </div>
      </div>

      {fullscreen && (
        <FullscreenShell
          title={
            <span className="flex items-center gap-2">
              <StatusBadge status={status} />
              {title ?? "Live-Ausgabe"}
            </span>
          }
          onClose={() => setFullscreen(false)}
          headerRight={
            running ? (
              <Button variant="danger" onClick={stop} className="px-2.5 py-1 text-xs">
                Stop
              </Button>
            ) : undefined
          }
        >
          <div
            ref={fsScrollRef}
            className="min-h-0 flex-1 overflow-y-auto whitespace-pre-wrap break-words rounded-lg border border-slate-800 bg-slate-950 p-4 font-mono text-sm leading-relaxed text-slate-200"
          >
            {body}
          </div>
        </FullscreenShell>
      )}
    </>
  );
}
