import { useEffect, useRef, useState } from "react";
import { api, getToken, wsUrl } from "../api";
import type { Task, TaskStatus, WsMessage } from "../types";
import { Button, StatusBadge } from "./ui";

export default function TaskConsole({
  taskId,
  onDone,
}: {
  taskId: string;
  onDone?: (task: Task) => void;
}) {
  const [lines, setLines] = useState("");
  const [status, setStatus] = useState<TaskStatus>("queued");
  const scrollRef = useRef<HTMLDivElement>(null);
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;

  useEffect(() => {
    setLines("");
    setStatus("queued");
    const token = getToken() ?? "";
    const ws = new WebSocket(
      wsUrl(`/api/ws/tasks/${taskId}?token=${encodeURIComponent(token)}`),
    );
    let manuallyClosed = false;

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
        onDoneRef.current?.(task);
      } else if (msg.type === "error") {
        const m = (msg as { message: string }).message;
        setLines((prev) => prev + `\n[Fehler] ${m}\n`);
      }
    };

    return () => {
      manuallyClosed = true;
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
        ws.close();
      }
      void manuallyClosed;
    };
  }, [taskId]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const running = status === "running" || status === "queued";

  async function stop() {
    try {
      await api.stopTask(taskId);
    } catch {
      /* ignore */
    }
  }

  return (
    <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-950">
      <div className="flex items-center justify-between border-b border-slate-800 px-3 py-2">
        <StatusBadge status={status} />
        {running && (
          <Button variant="danger" onClick={stop} className="px-2.5 py-1 text-xs">
            Stop
          </Button>
        )}
      </div>
      <div
        ref={scrollRef}
        className="max-h-96 min-h-24 overflow-y-auto whitespace-pre-wrap break-words p-3 font-mono text-xs leading-relaxed text-slate-200"
      >
        {lines || <span className="text-slate-600">Warte auf Ausgabe…</span>}
      </div>
    </div>
  );
}
