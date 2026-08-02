import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { Button, ErrorText, Spinner, formatDate } from "../components/ui";
import type {
  EnvProfile,
  HeartbeatIssueSeen,
  HeartbeatProjectStatus,
  HeartbeatStatus,
  Task,
} from "../types";

/** Dashboard-side heartbeat overview: a global toggle + per-project
 *  toggles + a "recent heartbeat-spawned tasks" feed.  Polls every 5s so
 *  the UI reflects the next tick within seconds without requiring a
 *  WebSocket for v1.
 */
export default function Heartbeat() {
  const [status, setStatus] = useState<HeartbeatStatus | null>(null);
  const [recent, setRecent] = useState<Task[]>([]);
  // Per-task env-profile options for the heartbeat table's per-project
  // select (so the operator can pick "zai" for project A and "default"
  // for project B independently). Defaults to an empty list when the
  // /api/env-profiles endpoint fails — the per-row select stays disabled
  // in that case.
  const [profiles, setProfiles] = useState<EnvProfile[]>([]);
  // Per-(project,issue) dashboard comment + close state, keyed so the
  // recent-tasks list can show "💬 vor 12 Min" / "✓ closed"
  // without a second fetch per row.
  const [issueStatus, setIssueStatus] = useState<
    Record<string, HeartbeatIssueSeen>
  >({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Per-issue inline "loading" flag for the action buttons so a slow
  // GitHub round-trip doesn't lock the whole page.
  const [issueBusy, setIssueBusy] = useState<Record<string, boolean>>({});

  const refresh = useCallback(async () => {
    try {
      const [s, r, profs] = await Promise.all([
        api.getHeartbeat(),
        fetchRecentHeartbeatTasks(),
        api.listEnvProfiles().catch(() => [] as EnvProfile[]),
      ]);
      setStatus(s);
      setRecent(r);
      setProfiles(profs);
      setError(null);
      // Walk the active projects' heartbeat_seen ledger so the UI can
      // show comment + close badges next to each recent task without a
      // second fetch per row.
      const projects = s.projects.filter((p) => p.enabled && p.github_full_name);
      const ledgers = await Promise.all(
        projects.map((p) =>
          api
            .listHeartbeatIssues(p.id)
            .then((rows) => rows.map((row) => ({ projectId: p.id, row })))
            .catch(() => [] as { projectId: string; row: HeartbeatIssueSeen }[]),
        ),
      );
      const next: Record<string, HeartbeatIssueSeen> = {};
      for (const entries of ledgers) {
        for (const { projectId, row } of entries) {
          next[`${projectId}:${row.issue_number}`] = row;
        }
      }
      setIssueStatus(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  async function runIssueAction(
    projectId: string,
    issueNumber: number,
    action: "comment-again" | "close" | "reopen",
  ) {
    const key = `${projectId}:${issueNumber}`;
    setIssueBusy((b) => ({ ...b, [key]: true }));
    try {
      if (action === "comment-again") {
        await api.commentAgainOnHeartbeatIssue(projectId, issueNumber);
      } else if (action === "close") {
        await api.closeHeartbeatIssue(projectId, issueNumber);
      } else {
        await api.reopenHeartbeatIssue(projectId, issueNumber);
      }
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setIssueBusy((b) => ({ ...b, [key]: false }));
    }
  }

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, 5000);
    return () => window.clearInterval(id);
  }, [refresh]);

  async function flipGlobal(enabled: boolean) {
    setBusy(true);
    try {
      await api.setHeartbeatEnabled(enabled);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function flipProject(projectId: string, enabled: boolean) {
    setBusy(true);
    try {
      await api.setProjectHeartbeatEnabled(projectId, enabled);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function setProjectEnvProfile(projectId: string, key: string) {
    setBusy(true);
    try {
      await api.setProjectHeartbeatEnvProfile(projectId, key);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function setGlobalEnvProfile(key: string) {
    setBusy(true);
    try {
      await api.setHeartbeatEnvProfile(key);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function setGlobalAgentKey(key: string) {
    setBusy(true);
    try {
      await api.setHeartbeatAgentKey(key);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function triggerNow() {
    setBusy(true);
    try {
      await api.triggerHeartbeat();
      // Poll once quickly so the UI picks up the just-fired tick.
      window.setTimeout(refresh, 1500);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (!status) {
    return (
      <div className="flex h-64 items-center justify-center text-slate-400">
        <Spinner className="h-6 w-6" />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl space-y-8 p-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold text-slate-100">
          🤖 Heartbeat
        </h1>
        <p className="text-sm text-slate-400">
          Periodically checks the open GitHub issues of active projects and
          automatically starts a {status.agent_key} task for each new issue,
          which investigates the problem and returns it as a PR titled
          <code className="ml-1 rounded bg-slate-800 px-1 py-0.5 text-xs">
            Fix #N: …
          </code>.
        </p>
      </header>

      {error && <ErrorText>{error}</ErrorText>}

      <section className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex flex-wrap items-center gap-3">
            <span
              className={`inline-flex h-3 w-3 rounded-full ${
                status.enabled
                  ? "bg-emerald-400 shadow-[0_0_8px_rgba(16,185,129,0.5)]"
                  : "bg-slate-600"
              }`}
              aria-hidden
            />
            <span className="text-base font-medium text-slate-100">
              {status.enabled ? "Heartbeat active" : "Heartbeat paused"}
            </span>
            <span className="text-xs text-slate-400">
              · Interval every {Math.round(status.interval_seconds / 60)} Min
              · Cooldown {status.cooldown_minutes} Min pro Project
            </span>
            {status.assignee_logins.length > 0 && (
              <span
                className="text-xs text-slate-400"
                title="Heartbeat only fixes issues assigned to one of these logins"
              >
                · Filtered to:{" "}
                {status.assignee_logins.map((a, i) => (
                  <span key={a}>
                    <span className="font-mono text-slate-200">@{a}</span>
                    {i < status.assignee_logins.length - 1 ? ", " : ""}
                  </span>
                ))}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              disabled={busy}
              onClick={triggerNow}
              title="Run a tick immediately (fire-and-forget)"
            >
              ▶ Run now
            </Button>
            {status.enabled ? (
              <Button
                variant="subtle"
                disabled={busy}
                onClick={() => flipGlobal(false)}
              >
                Pause
              </Button>
            ) : (
              <Button
                variant="primary"
                disabled={busy}
                onClick={() => flipGlobal(true)}
              >
                Activate
              </Button>
            )}
          </div>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2">
          <span className="flex items-center gap-2">
            <label className="text-xs uppercase tracking-wide text-slate-400">
              Agent
            </label>
            <select
              value={status.agent_key}
              onChange={(e) => setGlobalAgentKey(e.target.value)}
              disabled={busy || status.available_agent_keys.length <= 1}
              className="rounded-lg border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-100 outline-none focus:border-cyan-500 disabled:opacity-60"
              title={
                status.available_agent_keys.length <= 1
                  ? "No <agent>-host sibling enabled (set CD_<AGENT>_SSH_USER and restart)."
                  : "Determines which agent the heartbeat starts per issue. In-memory; resets on restart."
              }
            >
              {status.available_agent_keys.length === 0 ? (
                <option value={status.agent_key}>{status.agent_key}</option>
              ) : (
                status.available_agent_keys.map((k) => (
                  <option key={k} value={k}>
                    {k}
                    {k.endsWith("-host") ? " 🖥 host" : ""}
                  </option>
                ))
              )}
            </select>
            {status.agent_key.endsWith("-host") && (
              <span
                className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-xs font-medium text-emerald-300"
                title="Via SSH on the host (CD_<AGENT>_SSH_USER)"
              >
                🖥 host
              </span>
            )}
          </span>
          <span className="flex items-center gap-2">
            <label className="text-xs uppercase tracking-wide text-slate-400">
              Default env profile
            </label>
            <select
              value={status.env_profile_key}
              onChange={(e) => setGlobalEnvProfile(e.target.value)}
              disabled={busy || profiles.length === 0}
              className="rounded-lg border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-100 outline-none focus:border-cyan-500 disabled:opacity-60"
              title={
                profiles.length === 0
                  ? "No env profiles created (create one under /settings/env-profiles)."
                  : "Global default for auto-started tasks. Per-project override (right) takes precedence."
              }
            >
              <option value="">Default (CD_HEARTBEAT_ENV_PROFILE_KEY)</option>
              {profiles.map((pr) => (
                <option key={pr.key} value={pr.key}>
                  {pr.name}
                </option>
              ))}
            </select>
          </span>
        </div>
        <p className="mt-3 text-xs text-slate-500">
          The global toggle only applies in the running process. For a permanent default set{" "}
          <code className="rounded bg-slate-800 px-1 py-0.5">
            CD_HEARTBEAT_ENABLED=true
          </code>{" "}
          in the service configuration and restart.
        </p>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-medium text-slate-100">Projects</h2>
        {status.projects.length === 0 ? (
          <p className="rounded-2xl border border-slate-800 bg-slate-900/40 p-6 text-sm text-slate-400">
            No active projects with a GitHub link. Import a repo on the home
            page to see it here.
          </p>
        ) : (
          <div className="overflow-hidden rounded-2xl border border-slate-800">
            <table className="w-full text-left text-sm">
              <thead className="bg-slate-900/70 text-xs uppercase tracking-wide text-slate-400">
                <tr>
                  <th className="px-4 py-2.5">Project</th>
                  <th className="px-4 py-2.5">Repo</th>
                  <th className="px-4 py-2.5">Last tick</th>
                  <th className="px-4 py-2.5">Status</th>
                  <th className="px-4 py-2.5">Open</th>
                  <th className="px-4 py-2.5">Env profile</th>
                  <th className="px-4 py-2.5 text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {status.projects.map((p) => (
                  <ProjectRow
                    key={p.id}
                    p={p}
                    busy={busy}
                    profiles={profiles}
                    onToggle={(enabled) => flipProject(p.id, enabled)}
                    onSetEnvProfile={(key) => setProjectEnvProfile(p.id, key)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-medium text-slate-100">
          Recently auto-started tasks
        </h2>
        {recent.length === 0 ? (
          <p className="rounded-2xl border border-slate-800 bg-slate-900/40 p-6 text-sm text-slate-400">
            No heartbeat tasks yet. As soon as a new issue appears, the corresponding fix attempt shows up here.
          </p>
        ) : (
          <ul className="divide-y divide-slate-800 overflow-hidden rounded-2xl border border-slate-800">
            {recent.map((t) => (
              <RecentTaskRow
                key={t.id}
                t={t}
                seen={
                  t.heartbeat_issue_number != null
                    ? issueStatus[`${t.project_id}:${t.heartbeat_issue_number}`]
                    : undefined
                }
                issueBusy={
                  issueBusy[`${t.project_id}:${t.heartbeat_issue_number ?? ""}`] ?? false
                }
                onCommentAgain={() =>
                  runIssueAction(t.project_id, t.heartbeat_issue_number!, "comment-again")
                }
                onClose={() =>
                  runIssueAction(t.project_id, t.heartbeat_issue_number!, "close")
                }
                onReopen={() =>
                  runIssueAction(t.project_id, t.heartbeat_issue_number!, "reopen")
                }
              />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function ProjectRow({
  p,
  busy,
  profiles,
  onToggle,
  onSetEnvProfile,
}: {
  p: HeartbeatProjectStatus;
  busy: boolean;
  profiles: EnvProfile[];
  onToggle: (enabled: boolean) => void;
  onSetEnvProfile: (key: string) => void;
}) {
  const statusLabel = heartbeatStatusLabel(p);
  const statusColor = heartbeatStatusColor(p.last_heartbeat_status);
  return (
    <tr className="border-t border-slate-800 align-top">
      <td className="px-4 py-3">
        <a
          href={`#/projects/${p.id}`}
          className="font-medium text-slate-100 hover:text-cyan-300"
        >
          {p.name}
        </a>
      </td>
      <td className="px-4 py-3 text-xs text-slate-400">
        {p.github_full_name || (
          <span className="italic text-slate-600">no GitHub</span>
        )}
      </td>
      <td className="px-4 py-3 text-xs text-slate-400">
        {formatDate(p.last_heartbeat_at)}
      </td>
      <td className="px-4 py-3">
        <span
          className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs ${statusColor}`}
          title={p.last_heartbeat_error || undefined}
        >
          {statusLabel}
        </span>
        {p.last_heartbeat_error && (
          <p className="mt-1 max-w-xs truncate text-xs text-red-300">
            {p.last_heartbeat_error}
          </p>
        )}
      </td>
      <td className="px-4 py-3 text-xs text-slate-400">
        {p.inflight_task_ids.length > 0 ? (
          <span className="font-mono text-amber-300">
            {p.inflight_task_ids.length} running
          </span>
        ) : (
          <span className="text-slate-600">–</span>
        )}
      </td>
      <td className="px-4 py-3 text-xs">
        <select
          value={p.heartbeat_env_profile_key}
          onChange={(e) => onSetEnvProfile(e.target.value)}
          disabled={busy || profiles.length === 0}
          className="w-full max-w-[10rem] rounded-lg border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100 outline-none focus:border-cyan-500 disabled:opacity-60"
          title={
            profiles.length === 0
              ? "No env profiles created (create one under /settings/env-profiles)."
              : "Empty = default (CD_HEARTBEAT_ENV_PROFILE_KEY / no env overlay)"
          }
        >
          <option value="">Default (global / empty)</option>
          {profiles.map((pr) => (
            <option key={pr.key} value={pr.key}>
              {pr.name}
            </option>
          ))}
        </select>
      </td>
      <td className="px-4 py-3 text-right">
        <button
          type="button"
          disabled={busy}
          onClick={() => onToggle(!p.enabled)}
          className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
            p.enabled ? "bg-cyan-500" : "bg-slate-700"
          } ${busy ? "opacity-50" : ""}`}
          aria-pressed={p.enabled}
          title={p.enabled ? "Deactivate heartbeat for this project" : "Activate heartbeat for this project"}
        >
          <span
            className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
              p.enabled ? "translate-x-6" : "translate-x-1"
            }`}
          />
        </button>
      </td>
    </tr>
  );
}

function heartbeatStatusLabel(p: HeartbeatProjectStatus): string {
  if (!p.enabled) return "Off";
  if (!p.github_full_name) return "No repo";
  if (!p.last_heartbeat_status) return "Not yet checked";
  const map: Record<string, string> = {
    success: "Success",
    no_issues: "No new issues",
    cooldown: "Cooldown",
    disabled: "Off",
    error: "Error",
    skipped: "Skipped",
    no_github: "No repo",
  };
  return map[p.last_heartbeat_status] ?? p.last_heartbeat_status;
}

function heartbeatStatusColor(status: string): string {
  const map: Record<string, string> = {
    success: "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40",
    no_issues: "bg-slate-700 text-slate-300",
    cooldown: "bg-amber-500/20 text-amber-300 border border-amber-500/40",
    disabled: "bg-slate-800 text-slate-500",
    error: "bg-red-500/20 text-red-300 border border-red-500/40",
  };
  return map[status] ?? "bg-slate-800 text-slate-400";
}

/** Fetch the last 20 heartbeat-spawned tasks across all projects. There
 *  isn't a dedicated endpoint yet — we hit /running plus the per-project
 *  task lists. To keep the request count low in v1 we walk the running
 *  list once and then drop a single /tasks call per active project for
 *  the finished heartbeat-spawned tasks (the per-project call returns a
 *  flat list ordered by created_at desc).
 *  For a future iteration this should become a dedicated
 *  ``GET /api/heartbeat/recent-tasks`` endpoint.
 */
async function fetchRecentHeartbeatTasks(): Promise<Task[]> {
  try {
    const running = await api.listRunning();
    const seen = new Set<string>();
    const out: Task[] = [];
    for (const t of running) {
      if (!t.heartbeat_spawned) continue;
      seen.add(t.id);
      out.push(t);
      if (out.length >= 20) return out;
    }
    // Pull the rest from each active project's task list. Done serially
    // to avoid hammering the dashboard on a 5s poll cycle.
    const projects = await api.listProjects().catch(() => []);
    for (const p of projects) {
      if (out.length >= 20) break;
      try {
        const list = await api.listTasks(p.id);
        for (const t of list) {
          if (!t.heartbeat_spawned || seen.has(t.id)) continue;
          seen.add(t.id);
          out.push(t);
          if (out.length >= 20) break;
        }
      } catch {
        // ignore per-project failures so one bad project doesn't kill
        // the entire feed
      }
    }
    return out;
  } catch {
    return [];
  }
}

/** One row in the recent-tasks feed: the title link + result summary +
 *  the heartbeat comment / close state + three action buttons. Pulled
 *  out so the parent list stays readable and we can colocate the badge
 *  colour maps. */
function RecentTaskRow({
  t,
  seen,
  issueBusy,
  onCommentAgain,
  onClose,
  onReopen,
}: {
  t: Task;
  seen: HeartbeatIssueSeen | undefined;
  issueBusy: boolean;
  onCommentAgain: () => void;
  onClose: () => void;
  onReopen: () => void;
}) {
  const commentedAt = t.heartbeat_commented_at ?? seen?.last_commented_at ?? null;
  const closedAt = t.heartbeat_closed_at ?? seen?.last_issue_state_changed_at ?? null;
  const issueState = seen?.last_issue_state ?? "";
  const commentError = seen?.last_comment_error ?? "";
  const issueNumber = t.heartbeat_issue_number;
  return (
    <li className="bg-slate-900/40 px-4 py-3 text-sm">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <a
          href={`#/projects/${t.project_id}`}
          className="font-medium text-cyan-300 hover:text-cyan-200"
        >
          🤖 Fix #{issueNumber ?? "?"}
        </a>
        <span className="text-xs text-slate-500">
          {formatDate(t.created_at)}
        </span>
      </div>
      <p className="mt-1 line-clamp-2 text-xs text-slate-400">
        {t.result_summary || t.prompt.slice(0, 240)}
      </p>
      <p className="mt-1 text-xs text-slate-500">
        Agent: <code className="text-slate-300">{t.agent}</code> · Status:{" "}
        <code className="text-slate-300">{t.status}</code>
      </p>

      {(commentedAt || closedAt || commentError || issueState) && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          {commentedAt && (
            <a
              href={seen?.last_comment_url || "#"}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 rounded-full bg-cyan-500/20 px-2 py-0.5 text-cyan-200 hover:bg-cyan-500/30"
              title={seen?.last_comment_url || undefined}
            >
              💬 Comment · {formatDate(commentedAt)}
            </a>
          )}
          {issueState === "closed" && (
            <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/20 px-2 py-0.5 text-emerald-300">
              ✓ closed{closedAt ? ` · ${formatDate(closedAt)}` : ""}
            </span>
          )}
          {issueState === "open" && (
            <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/20 px-2 py-0.5 text-amber-300">
              ↻ reopened{closedAt ? ` · ${formatDate(closedAt)}` : ""}
            </span>
          )}
          {!commentedAt && !issueState && seen && (
            <span className="inline-flex items-center gap-1 rounded-full bg-slate-800 px-2 py-0.5 text-slate-400">
              no comment yet
            </span>
          )}
          {commentError && (
            <span
              className="inline-flex items-center gap-1 rounded-full bg-red-500/20 px-2 py-0.5 text-red-300"
              title={commentError}
            >
              ⚠ GitHub-Error
            </span>
          )}
        </div>
      )}

      {issueNumber != null && (
        <div className="mt-2 flex flex-wrap gap-2">
          <Button
            variant="ghost"
            disabled={issueBusy}
            onClick={onCommentAgain}
            title="Post another dashboard comment on the issue"
          >
            💬 Comment again
          </Button>
          {issueState === "closed" ? (
            <Button
              variant="ghost"
              disabled={issueBusy}
              onClick={onReopen}
              title="Reopen the issue so the heartbeat will reprocess it"
            >
              ↻ Reopen
            </Button>
          ) : (
            <Button
              variant="ghost"
              disabled={issueBusy}
              onClick={onClose}
              title="Close the issue manually"
            >
              ✓ Close
            </Button>
          )}
        </div>
      )}
    </li>
  );
}