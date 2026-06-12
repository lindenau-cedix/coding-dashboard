import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ensureCloudflareAccess, wsSessionUrl } from "../api";
import { Button, Modal, Spinner, StatusBadge } from "../components/ui";
import type { Agent, Project, SessionMessage, SessionWsMessage } from "../types";

export default function SessionPage() {
  const { id: projectId = "", taskId = "" } = useParams();
  const navigate = useNavigate();

  const [project, setProject] = useState<Project | null>(null);
  const [agent, setAgent] = useState<Agent | null>(null);
  const [status, setStatus] = useState("running");
  const [ended, setEnded] = useState(false);
  const [summary, setSummary] = useState("");
  const [commitHash, setCommitHash] = useState("");
  const [pushed, setPushed] = useState(false);

  // Chat messages accumulated from WS and history.
  const [messages, setMessages] = useState<SessionMessage[]>([]);
  // Streaming output not yet in a message.
  const [streamingOutput, setStreamingOutput] = useState("");
  const [gitOutput, setGitOutput] = useState("");

  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);

  const [showEndModal, setShowEndModal] = useState(false);
  const [commitMessage, setCommitMessage] = useState("");
  const [ending, setEnding] = useState(false);
  const [endError, setEndError] = useState("");

  const chatEndRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Load project + agents once.
  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const [p, ag] = await Promise.all([
          api.getProject(projectId),
          api.agents(),
        ]);
        if (!active) return;
        setProject(p);
        const sess = await api.getSession(taskId);
        if (!active) return;
        if (sess.chat_history?.length) {
          setMessages(sess.chat_history);
        }
        setStatus(sess.status);
        if (sess.status === "success" || sess.status === "failed") {
          setEnded(true);
          setSummary(sess.result_summary || "");
        }
        const found = ag.find((a) => a.key === sess.agent);
        if (found) setAgent(found);
      } catch {
        // Session not found / not accessible — redirect back.
        navigate(`/projects/${projectId}`);
      }
    })();
    return () => { active = false; };
  }, [projectId, taskId, navigate]);

  // WebSocket connection.
  useEffect(() => {
    let ws: WebSocket | null = null;
    let manuallyClosed = false;

    void (async () => {
      try {
        await ensureCloudflareAccess();
      } catch {
        return;
      }
      if (manuallyClosed) return;

      ws = new WebSocket(wsSessionUrl(taskId));
      wsRef.current = ws;

      ws.onmessage = (ev) => {
        let msg: SessionWsMessage;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.type === "started") {
          // Session is live.
        } else if (msg.type === "output") {
          const data = (msg as { data: string }).data;
          setStreamingOutput((prev) => prev + data);
        } else if (msg.type === "message") {
          const m = msg as { role: "user" | "assistant"; content: string };
          setMessages((prev) => [
            ...prev,
            { role: m.role, content: m.content, timestamp: new Date().toISOString() },
          ]);
          setStreamingOutput(""); // Flush any pending streaming output.
        } else if (msg.type === "status") {
          const s = (msg as { status: string }).status;
          setStatus(s);
        } else if (msg.type === "git") {
          const data = (msg as { data: string }).data;
          setGitOutput((prev) => prev + data);
        } else if (msg.type === "done") {
          const d = msg as { status: string; summary?: string };
          setStatus(d.status);
          setSummary(d.summary || "");
          setEnded(true);
          setStreamingOutput("");
          // Flush any remaining streaming as an assistant message.
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            if (last?.role === "assistant" && last.content === "") {
              return prev;
            }
            if (streamingOutput) {
              return [
                ...prev,
                {
                  role: "assistant",
                  content: streamingOutput,
                  timestamp: new Date().toISOString(),
                },
              ];
            }
            return prev;
          });
        }
      };

      ws.onerror = () => {
        // Connection error — not fatal.
      };

      ws.onclose = () => {
        if (!ended) {
          // Unexpected disconnect — allow rejoin by re-fetching.
          void api.getSession(taskId).then((sess) => {
            if (sess.chat_history?.length && messages.length === 0) {
              setMessages(sess.chat_history);
            }
            setStatus(sess.status);
            if (sess.status !== "running") setEnded(true);
          });
        }
      };
    })();

    return () => {
      manuallyClosed = true;
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        ws.close();
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId]);

  // Auto-scroll chat.
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streamingOutput]);

  async function sendMessage() {
    const text = input.trim();
    if (!text || sending || ended) return;
    setInput("");
    setSending(true);
    try {
      wsRef.current?.send(JSON.stringify({ type: "message", content: text }));
    } finally {
      setSending(false);
      inputRef.current?.focus();
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void sendMessage();
    }
  }

  async function handleEndSession() {
    setEnding(true);
    setEndError("");
    try {
      const result = await api.endSession(taskId, commitMessage);
      setSummary(result.summary || "");
      setCommitHash(result.commit_hash || "");
      setPushed(result.pushed);
      setEnded(true);
      setShowEndModal(false);
      wsRef.current?.send(JSON.stringify({ type: "end", commit_message: commitMessage }));
    } catch (err) {
      setEndError(err instanceof Error ? err.message : "Beenden fehlgeschlagen");
    } finally {
      setEnding(false);
    }
  }

  const isLive = status === "running" && !ended;

  return (
    <div className="flex flex-col h-full space-y-4">
      {/* Header */}
      <div>
        <Link to={`/projects/${projectId}`} className="text-sm text-slate-400 hover:text-cyan-400">
          ← {project?.name || "Projekt"}
        </Link>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <h1 className="text-xl font-semibold text-slate-100">
              {agent?.display_name ?? "Session"}
            </h1>
            <StatusBadge status={status} />
            {isLive && (
              <span className="rounded bg-cyan-500/15 px-2 py-0.5 text-xs font-medium text-cyan-300">
                Live
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            {!ended && isLive && (
              <Button variant="danger" onClick={() => setShowEndModal(true)}>
                Session beenden
              </Button>
            )}
            {ended && (
              <Link
                to={`/projects/${projectId}`}
                className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-700"
              >
                ← Zurück zum Projekt
              </Link>
            )}
          </div>
        </div>
        {agent && (
          <p className="mt-1 text-sm text-slate-400">
            {agent.model_choices?.length ? `Modell: ${agent.model_choices.join(", ")}` : ""}
          </p>
        )}
      </div>

      {/* Chat area */}
      <div className="flex-1 overflow-hidden rounded-xl border border-slate-800 bg-slate-900 flex flex-col min-h-96">
        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {messages.length === 0 && !streamingOutput && (
            <p className="text-sm text-slate-500 italic">
              {isLive
                ? "Warte auf erste Antwort des Agenten…"
                : "Keine Nachrichten in dieser Session."}
            </p>
          )}

          {messages.map((msg, i) => (
            <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
              <div
                className={`max-w-[80%] rounded-2xl px-4 py-2.5 text-sm whitespace-pre-wrap ${
                  msg.role === "user"
                    ? "bg-cyan-600 text-white rounded-br-md"
                    : "bg-slate-800 text-slate-200 rounded-bl-md"
                }`}
              >
                {msg.content}
              </div>
            </div>
          ))}

          {/* Streaming output from assistant (accumulated until next message or done). */}
          {streamingOutput && (
            <div className="flex justify-start">
              <div className="max-w-[80%] rounded-2xl rounded-bl-md bg-slate-800 px-4 py-2.5 text-sm whitespace-pre-wrap text-slate-200 font-mono">
                {streamingOutput}
                <span className="ml-1 inline-block h-3 w-2 animate-pulse rounded-sm bg-slate-400 align-middle" />
              </div>
            </div>
          )}

          <div ref={chatEndRef} />
        </div>

        {/* Git output log (shown after end). */}
        {gitOutput && (
          <div className="border-t border-slate-800 px-4 py-2 font-mono text-xs text-slate-400 whitespace-pre">
            {gitOutput}
          </div>
        )}

        {/* Input bar */}
        {isLive && (
          <div className="border-t border-slate-800 p-3 flex gap-2 items-end">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              rows={2}
              placeholder="Nachricht an den Agenten… (Enter zum Senden, Shift+Enter für neue Zeile)"
              className="flex-1 resize-none rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-500 placeholder-slate-500"
            />
            <Button
              onClick={() => void sendMessage()}
              disabled={!input.trim() || sending}
              className="mb-0.5"
            >
              {sending ? <Spinner className="h-4 w-4" /> : "Senden"}
            </Button>
          </div>
        )}

        {/* After-end summary bar. */}
        {ended && summary && (
          <div className="border-t border-slate-800 px-4 py-3 space-y-1">
            <div className="text-xs uppercase tracking-wide text-slate-500">Ergebnis</div>
            <p className="text-sm text-slate-300 whitespace-pre-wrap">{summary}</p>
            {commitHash && (
              <div className="flex items-center gap-2 text-xs text-slate-400 mt-1">
                <span>Commit:</span>
                {pushed ? (
                  <a
                    href={project?.github_url ? `${project.github_url}/commit/${commitHash}` : "#"}
                    target="_blank"
                    rel="noreferrer"
                    className="text-cyan-400 hover:underline"
                  >
                    {commitHash.slice(0, 8)}
                  </a>
                ) : (
                  <span className="font-mono">{commitHash.slice(0, 8)}</span>
                )}
                {pushed && <span>✓ gepusht</span>}
              </div>
            )}
          </div>
        )}
      </div>

      {/* End session modal */}
      {showEndModal && (
        <Modal title="Session beenden" onClose={() => setShowEndModal(false)}>
          <div className="space-y-4">
            <p className="text-sm text-slate-400">
              Die Session wird beendet, der Chatverlauf gespeichert, und das Repository
              (falls vorhanden) wird automatisch committet und gepusht.
            </p>
            <div>
              <label className="mb-1 block text-sm text-slate-400">
                Commit-Nachricht (optional)
              </label>
              <textarea
                value={commitMessage}
                onChange={(e) => setCommitMessage(e.target.value)}
                rows={3}
                placeholder="z.B. Feature X implementiert nach Session mit dem Agenten"
                className="w-full resize-none rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-500"
              />
            </div>
            {endError && (
              <p className="rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                {endError}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setShowEndModal(false)}>
                Abbrechen
              </Button>
              <Button variant="danger" onClick={() => void handleEndSession()} disabled={ending}>
                {ending ? <Spinner className="h-4 w-4" /> : "Session beenden"}
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
