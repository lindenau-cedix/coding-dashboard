import { useEffect, useMemo, useRef, useState } from "react";
import { api, commitUrl, ensureCloudflareAccess, wsSessionUrl } from "../api";
import type { Agent, Project, SessionWsMessage, TaskStatus } from "../types";
import { Button, ErrorText, IconButton, Spinner, StatusBadge } from "./ui";

const TERMINAL_STATUSES = new Set(["success", "failed", "error", "interrupted", "cancelled"]);
type ConnectionState = "idle" | "connecting" | "open" | "closed" | "error";

function isDone(status: string): boolean {
  return TERMINAL_STATUSES.has(status);
}

function blankLine(cols: number): string[] {
  return Array.from({ length: cols }, () => " ");
}

function clamp(n: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, n));
}

function renderTerminal(raw: string, cols: number): string {
  const width = clamp(cols, 20, 300);
  const maxLines = 2000;
  let lines: string[][] = [blankLine(width)];
  let row = 0;
  let col = 0;
  let savedRow = 0;
  let savedCol = 0;

  function ensureRow(idx: number) {
    while (lines.length <= idx) lines.push(blankLine(width));
  }

  function trimScrollback() {
    if (lines.length <= maxLines) return;
    const drop = lines.length - maxLines;
    lines = lines.slice(drop);
    row = Math.max(0, row - drop);
    savedRow = Math.max(0, savedRow - drop);
  }

  function putChar(ch: string) {
    if (col >= width) {
      col = 0;
      row += 1;
    }
    ensureRow(row);
    lines[row][col] = ch;
    col += 1;
    trimScrollback();
  }

  function eraseLine(mode: number) {
    ensureRow(row);
    if (mode === 1) {
      for (let i = 0; i <= col; i += 1) lines[row][i] = " ";
    } else if (mode === 2) {
      lines[row] = blankLine(width);
    } else {
      for (let i = col; i < width; i += 1) lines[row][i] = " ";
    }
  }

  function eraseDisplay(mode: number) {
    if (mode === 2 || mode === 3) {
      lines = [blankLine(width)];
      row = 0;
      col = 0;
      return;
    }
    ensureRow(row);
    if (mode === 1) {
      for (let r = 0; r < row; r += 1) lines[r] = blankLine(width);
      for (let c = 0; c <= col; c += 1) lines[row][c] = " ";
      return;
    }
    for (let c = col; c < width; c += 1) lines[row][c] = " ";
    for (let r = row + 1; r < lines.length; r += 1) lines[r] = blankLine(width);
  }

  function parseParams(body: string): number[] {
    const clean = body.replace(/^\?/, "");
    if (!clean) return [];
    return clean.split(";").map((part) => {
      const n = Number.parseInt(part, 10);
      return Number.isFinite(n) ? n : 0;
    });
  }

  function applyCsi(body: string, final: string) {
    const p = parseParams(body);
    const n = p[0] || 1;
    if (final === "A") row = Math.max(0, row - n);
    else if (final === "B") row += n;
    else if (final === "C") col = Math.min(width - 1, col + n);
    else if (final === "D") col = Math.max(0, col - n);
    else if (final === "E") {
      row += n;
      col = 0;
    } else if (final === "F") {
      row = Math.max(0, row - n);
      col = 0;
    } else if (final === "G") {
      col = clamp(n - 1, 0, width - 1);
    } else if (final === "H" || final === "f") {
      row = Math.max(0, (p[0] || 1) - 1);
      col = clamp((p[1] || 1) - 1, 0, width - 1);
    } else if (final === "J") {
      eraseDisplay(p[0] || 0);
    } else if (final === "K") {
      eraseLine(p[0] || 0);
    } else if (final === "s") {
      savedRow = row;
      savedCol = col;
    } else if (final === "u") {
      row = savedRow;
      col = savedCol;
    }
    ensureRow(row);
    trimScrollback();
  }

  for (let i = 0; i < raw.length; i += 1) {
    const ch = raw[i];
    if (ch === "\x1b") {
      const next = raw[i + 1];
      if (next === "[") {
        let j = i + 2;
        while (j < raw.length && !/[A-Za-z~]/.test(raw[j])) j += 1;
        if (j < raw.length) {
          applyCsi(raw.slice(i + 2, j), raw[j]);
          i = j;
        }
      } else if (next === "]") {
        let j = i + 2;
        while (j < raw.length && raw[j] !== "\x07" && !(raw[j] === "\x1b" && raw[j + 1] === "\\")) {
          j += 1;
        }
        i = j;
      } else if (next === "7") {
        savedRow = row;
        savedCol = col;
        i += 1;
      } else if (next === "8") {
        row = savedRow;
        col = savedCol;
        i += 1;
      } else if (next === "c") {
        lines = [blankLine(width)];
        row = 0;
        col = 0;
        i += 1;
      } else if (next === "(" || next === ")") {
        i += 2;
      } else {
        i += 1;
      }
    } else if (ch === "\r") {
      col = 0;
    } else if (ch === "\n") {
      row += 1;
      col = 0;
      ensureRow(row);
      trimScrollback();
    } else if (ch === "\b" || ch === "\x7f") {
      col = Math.max(0, col - 1);
    } else if (ch === "\t") {
      const nextTab = Math.min(width, col + (8 - (col % 8)));
      while (col < nextTab) putChar(" ");
    } else if (ch >= " ") {
      putChar(ch);
    }
  }

  return lines.map((line) => line.join("").replace(/\s+$/, "")).join("\n").replace(/\s+$/, "");
}

function plainTerminalFallback(raw: string): string {
  return raw
    .replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, "")
    .replace(/\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])/g, "")
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/g, "")
    .trim();
}

function keyToBytes(e: React.KeyboardEvent): string | null {
  if (e.metaKey) return null;
  // NEVER forward Ctrl+V / Ctrl+Shift+V (with or without Alt) to the agent TUI
  // as the raw \x16 byte. Modern full-screen TUIs (Claude Code, Codex, Hermes,
  // …) treat Ctrl+V as the OS-clipboard image-paste shortcut and try to read
  // an image from `arboard` / X11 / Wayland. Our headless PTY has no display
  // server, so that lookup hangs/fails and the user sees errors like
  // "Failed to paste image: clipboard unavailable: X11 server connection
  // timed out because it was unreachable" (Codex), "no image found" (Claude
  // Code) or a silent no-op (Hermes). Returning `null` here lets the browser
  // fall through to its default `paste` event so the onPaste handler can
  // deliver the text as a bracketed paste, which is the only path that works
  // in a headless PTY. Ctrl+Alt+V is already filtered by the `!e.altKey`
  // guard below and Ctrl+Insert is harmless (we send \x1b[2~) but also gets
  // no image handler in the common TUIs.
  if (e.ctrlKey && !e.metaKey) {
    const k = e.key.toLowerCase();
    if (k === "v") return null;
  }
  if (e.ctrlKey && !e.altKey && e.key.length === 1) {
    const code = e.key.toUpperCase().charCodeAt(0) - 64;
    if (code >= 1 && code <= 26) return String.fromCharCode(code);
  }
  switch (e.key) {
    case "Enter":
      return "\r";
    case "Backspace":
      return "\x7f";
    case "Tab":
      return "\t";
    case "Escape":
      return "\x1b";
    case "ArrowUp":
      return "\x1b[A";
    case "ArrowDown":
      return "\x1b[B";
    case "ArrowRight":
      return "\x1b[C";
    case "ArrowLeft":
      return "\x1b[D";
    case "Home":
      return "\x1b[H";
    case "End":
      return "\x1b[F";
    case "Delete":
      return "\x1b[3~";
    case "Insert":
      return "\x1b[2~";
    case "PageUp":
      return "\x1b[5~";
    case "PageDown":
      return "\x1b[6~";
    default:
      if (!e.ctrlKey && !e.altKey && e.key.length === 1) return e.key;
      return null;
  }
}

export default function SessionTerminalModal({
  project,
  agents,
  taskId,
  onClose,
  onEnded,
}: {
  project: Project;
  agents: Agent[];
  taskId: string;
  onClose: () => void;
  onEnded: () => void;
}) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [agentKey, setAgentKey] = useState("");
  const [startArgs, setStartArgs] = useState("");
  const [status, setStatus] = useState<TaskStatus>("running");
  const [rawOutput, setRawOutput] = useState("");
  const [gitOutput, setGitOutput] = useState("");
  const [summary, setSummary] = useState("");
  const [commitHash, setCommitHash] = useState("");
  const [pushed, setPushed] = useState(false);
  const [commitMessage, setCommitMessage] = useState("");
  const [ending, setEnding] = useState(false);
  const [connectionState, setConnectionState] = useState<ConnectionState>("idle");
  const [terminalSize, setTerminalSize] = useState({ cols: 100, rows: 30 });
  const [expanded, setExpanded] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const rawRef = useRef("");
  const onEndedRef = useRef(onEnded);
  const terminalRef = useRef<HTMLDivElement>(null);

  const agentName = agents.find((a) => a.key === agentKey)?.display_name ?? agentKey;
  const live = status === "running" && !ending;
  const screen = useMemo(
    () => renderTerminal(rawOutput, terminalSize.cols),
    [rawOutput, terminalSize.cols],
  );
  const terminalText = useMemo(() => {
    if (loading) return "Lädt...";
    if (screen.trim()) return screen;
    if (rawOutput.trim()) {
      return plainTerminalFallback(rawOutput) || "Terminalausgabe enthält nur Steuersequenzen.";
    }
    if (connectionState === "connecting") return "Verbinde mit Terminal...";
    if (connectionState === "open") return "Verbunden. Warte auf erste Ausgabe...";
    if (connectionState === "error") return "Terminal-Verbindung fehlgeschlagen.";
    if (connectionState === "closed" && !isDone(status)) {
      return "Terminal-Verbindung geschlossen. Session erneut öffnen.";
    }
    if (isDone(status)) return "Keine Terminalausgabe gespeichert.";
    return "Terminal wird gestartet...";
  }, [connectionState, loading, rawOutput, screen, status]);

  function appendOutput(data: string, offset?: number) {
    setRawOutput((prev) => {
      const start = typeof offset === "number" ? offset : prev.length;
      let next = prev;
      if (start < prev.length) {
        next = prev + data.slice(prev.length - start);
      } else {
        next = prev + data;
      }
      rawRef.current = next;
      return next;
    });
  }

  function sendResize() {
    const el = terminalRef.current;
    const ws = wsRef.current;
    if (!el || !ws || ws.readyState !== WebSocket.OPEN) return;
    const rect = el.getBoundingClientRect();
    const cols = clamp(Math.floor((rect.width - 24) / 8), 20, 300);
    const rows = clamp(Math.floor((rect.height - 24) / 18), 5, 120);
    setTerminalSize((prev) => (prev.cols === cols && prev.rows === rows ? prev : { cols, rows }));
    ws.send(JSON.stringify({ type: "resize", cols, rows }));
  }

  function sendBytes(bytes: string) {
    const ws = wsRef.current;
    if (!live || !bytes || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "message", content: bytes }));
  }

  useEffect(() => {
    onEndedRef.current = onEnded;
  }, [onEnded]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    (async () => {
      try {
        const sess = await api.getSession(taskId);
        if (!active) return;
        const output = sess.output || "";
        rawRef.current = output;
        setRawOutput(output);
        setAgentKey(sess.agent);
        setStartArgs(sess.start_args || "");
        setStatus(sess.status as TaskStatus);
        setSummary(sess.result_summary || "");
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : "Session konnte nicht geladen werden");
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [taskId]);

  useEffect(() => {
    if (loading || isDone(status)) return;
    let ws: WebSocket | null = null;
    let closed = false;
    (async () => {
      setConnectionState("connecting");
      try {
        await ensureCloudflareAccess();
      } catch (err) {
        if (!closed) {
          setConnectionState("error");
          setError(err instanceof Error ? err.message : "Terminal-Verbindung konnte nicht vorbereitet werden");
        }
        return;
      }
      if (closed) return;
      ws = new WebSocket(wsSessionUrl(taskId, rawRef.current.length));
      wsRef.current = ws;
      ws.onopen = () => {
        setConnectionState("open");
        terminalRef.current?.focus();
        sendResize();
      };
      ws.onerror = () => {
        if (!closed) {
          setConnectionState("error");
          setError("Terminal-WebSocket konnte nicht geöffnet werden.");
        }
      };
      ws.onclose = () => {
        if (!closed) setConnectionState("closed");
      };
      ws.onmessage = (ev) => {
        let msg: SessionWsMessage;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.type === "output") {
          const out = msg as { data: string; offset?: number };
          appendOutput(out.data, out.offset);
        } else if (msg.type === "status") {
          const nextStatus = (msg as { status: TaskStatus }).status;
          setStatus(nextStatus);
          if (isDone(nextStatus)) setConnectionState("closed");
        } else if (msg.type === "git") {
          setGitOutput((prev) => prev + (msg as { data: string }).data);
        } else if (msg.type === "done") {
          const done = msg as {
            status: TaskStatus;
            summary?: string;
            commit_hash?: string;
            pushed?: boolean;
          };
          setStatus(done.status);
          setConnectionState("closed");
          setSummary(done.summary || "");
          setCommitHash(done.commit_hash || "");
          setPushed(Boolean(done.pushed));
          onEndedRef.current();
        } else if (msg.type === "error") {
          setError((msg as { message: string }).message);
        }
      };
    })();

    return () => {
      closed = true;
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        ws.close();
      }
    };
  }, [loading, status, taskId]);

  useEffect(() => {
    const el = terminalRef.current;
    if (!el) return;
    const resize = () => sendResize();
    resize();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", resize);
      return () => window.removeEventListener("resize", resize);
    }
    const observer = new ResizeObserver(resize);
    observer.observe(el);
    return () => observer.disconnect();
  }, [taskId]);

  useEffect(() => {
    const el = terminalRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [terminalText]);

  async function endSession() {
    setEnding(true);
    setError("");
    try {
      const result = await api.endSession(taskId, commitMessage);
      setStatus(result.status as TaskStatus);
      setSummary(result.summary || "");
      setCommitHash(result.commit_hash || "");
      setPushed(Boolean(result.pushed));
      onEndedRef.current();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Session konnte nicht beendet werden");
    } finally {
      setEnding(false);
    }
  }

  function onTerminalKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    const bytes = keyToBytes(e);
    if (bytes === null) return;
    e.preventDefault();
    sendBytes(bytes);
  }

  function onTerminalPaste(e: React.ClipboardEvent<HTMLDivElement>) {
    // Prefer text/plain — anything else (text/html, files, images) would need
    // OS-clipboard support the headless PTY can't provide, so we drop it
    // instead of forwarding a paste the TUI can't honour.
    const text = e.clipboardData.getData("text/plain") || e.clipboardData.getData("text");
    if (!text) return;
    e.preventDefault();
    // Wrap in DEC bracketed-paste sequences so full-screen TUIs (Claude Code,
    // Codex, Hermes, …) treat the whole clipboard as a single paste event.
    // Without this each newline in the pasted text is interpreted as Enter
    // and submits the prompt prematurely. The PTY was put into mode ?2004h
    // by the backend on session start; if the TUI doesn't honour it, the
    // surrounding escape sequences are still harmless noise. This is reached
    // only via the browser's own paste event (Ctrl+V / right-click → Paste),
    // because keyToBytes returns null for Ctrl+V so the browser default
    // survives and synthesises the paste event with the real clipboard text.
    sendBytes(`\x1b[200~${text}\x1b[201~`);
  }

  return (
    <div
      className={`fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm ${
        expanded ? "p-0" : "p-3"
      }`}
    >
      <div
        className={`flex w-full flex-col border border-slate-700 bg-slate-950 shadow-2xl ${
          expanded
            ? "h-full max-w-none rounded-none"
            : "h-[min(860px,calc(100vh-1.5rem))] max-w-6xl rounded-xl"
        }`}
      >
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-4 py-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="truncate text-base font-semibold text-slate-100">
                {agentName || "Session"}
              </h2>
              <StatusBadge status={status} />
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
              <span>{project.name}</span>
              {startArgs && <span className="max-w-xl truncate font-mono">{startArgs}</span>}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <IconButton
              label={expanded ? "Vollbild verlassen" : "Vollbild"}
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? "🗗" : "⛶"}
            </IconButton>
            <Button variant="ghost" onClick={onClose}>
              Schließen
            </Button>
            {live && (
              <Button variant="danger" onClick={() => void endSession()} disabled={ending}>
                {ending ? <Spinner className="h-4 w-4" /> : "Session beenden"}
              </Button>
            )}
          </div>
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-3 p-4">
          <ErrorText>{error}</ErrorText>
          <div
            ref={terminalRef}
            tabIndex={0}
            onKeyDown={onTerminalKeyDown}
            onPaste={onTerminalPaste}
            onMouseDown={() => terminalRef.current?.focus()}
            className="min-h-0 flex-1 overflow-auto rounded-lg border border-slate-800 bg-black p-3 font-mono text-[13px] leading-5 whitespace-pre text-green-300 outline-none focus:border-cyan-500"
          >
            {terminalText}
          </div>
          {gitOutput && (
            <pre className="max-h-24 overflow-auto rounded-lg border border-slate-800 bg-slate-900 p-3 font-mono text-xs whitespace-pre-wrap text-slate-300">
              {gitOutput}
            </pre>
          )}
          {live ? (
            <div className="flex flex-wrap items-end gap-2">
              <label className="min-w-0 flex-1">
                <span className="mb-1 block text-xs text-slate-500">Commit-Nachricht</span>
                <input
                  value={commitMessage}
                  onChange={(e) => setCommitMessage(e.target.value)}
                  className="w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-500"
                />
              </label>
            </div>
          ) : (
            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-800 pt-3 text-sm text-slate-300">
              <span>{summary || "Session beendet"}</span>
              {commitHash && (
                <span className="flex items-center gap-2 text-xs text-slate-400">
                  {commitUrl(project, commitHash) ? (
                    <a
                      href={commitUrl(project, commitHash)!}
                      target="_blank"
                      rel="noreferrer"
                      className="font-mono text-cyan-400 hover:underline"
                    >
                      {commitHash.slice(0, 8)}
                    </a>
                  ) : (
                    <span className="font-mono">{commitHash.slice(0, 8)}</span>
                  )}
                  <span>{pushed ? "gepusht" : "nicht gepusht"}</span>
                </span>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
