import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import NewProjectModal from "../components/NewProjectModal";
import RunningAgents from "../components/RunningAgents";
import SyncFromGithubModal from "../components/SyncFromGithubModal";
import { Button, ErrorText, Spinner, formatDate, parseApiDate } from "../components/ui";
import type { Project } from "../types";

type ArchiveFilter = "active" | "archived";

export default function Projects() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [archiveFilter, setArchiveFilter] = useState<ArchiveFilter>("active");
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState("");
  const [showModal, setShowModal] = useState(false);
  const [showSync, setShowSync] = useState(false);

  const load = useCallback(async () => {
    try {
      const archived = archiveFilter === "archived" ? "true" : "false";
      setProjects(await api.listProjects(archived));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Load failed");
    }
  }, [archiveFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  async function remove(p: Project) {
    if (
      !confirm(
        `Remove project "${p.name}" locally?\n(The GitHub repository stays.)`,
      )
    )
      return;
    try {
      await api.deleteProject(p.id, false);
      setProjects((prev) => prev?.filter((x) => x.id !== p.id) ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("projects.deleteFailed"));
    }
  }

  async function toggleArchive(p: Project) {
    const wasArchived = p.archived;
    if (wasArchived) {
      // Restoring is reversible; no confirm needed.
    } else if (
      !confirm(t("projects.archiveConfirm", { name: p.name }))
    ) {
      return;
    }
    try {
      const updated = wasArchived
        ? await api.unarchiveProject(p.id)
        : await api.archiveProject(p.id);
      // Drop the card from the current view (the filter no longer matches
      // after archive/unarchive); a reload afterwards reconciles the
      // inverse view so the other tab stays consistent.
      setProjects((prev) =>
        prev?.filter((x) => x.id !== p.id) ?? null,
      );
      void load();
      setError("");
    } catch (err) {
      const verb = wasArchived ? "Restore" : "Archive";
      setError(
        `${verb} failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }

  const emptyHint =
    archiveFilter === "active"
      ? {
          title: "No active projects yet.",
          body: (
            <>
              Create a new one, import an existing GitHub repo, or clone all
              your repos at once via <em>{t("projects.syncFromGithub")}</em>.
            </>
          ),
        }
      : {
          title: t("projects.noArchived"),
          body: (
            <>
              {t("projects.emptyArchivedHint", { icon: "📦" })}
            </>
          ),
        };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-2xl font-semibold text-slate-100">{t("projects.title")}</h1>
        <div className="flex items-center gap-2">
          {/* Archive toggle: two pills, mutually exclusive. */}
          <div
            role="tablist"
            aria-label="Project filter"
            className="inline-flex overflow-hidden rounded-lg border border-slate-700"
          >
            <button
              role="tab"
              aria-selected={archiveFilter === "active"}
              onClick={() => setArchiveFilter("active")}
              className={`px-3 py-1.5 text-sm transition ${
                archiveFilter === "active"
                  ? "bg-cyan-600 text-white"
                  : "bg-slate-800 text-slate-300 hover:bg-slate-700"
              }`}
            >
              {t("projects.active")}
            </button>
            <button
              role="tab"
              aria-selected={archiveFilter === "archived"}
              onClick={() => setArchiveFilter("archived")}
              className={`px-3 py-1.5 text-sm transition ${
                archiveFilter === "archived"
                  ? "bg-cyan-600 text-white"
                  : "bg-slate-800 text-slate-300 hover:bg-slate-700"
              }`}
            >
              📦 {t("projects.archived")}
            </button>
          </div>
          <Button variant="ghost" onClick={() => setShowSync(true)}>
            ⇣ {t("projects.syncFromGithub")}
          </Button>
          <Button onClick={() => setShowModal(true)}>+ {t("projects.newProject")}</Button>
        </div>
      </div>

      <ErrorText>{error}</ErrorText>

      <RunningAgents />

      {projects === null ? (
        <div className="flex justify-center py-16 text-slate-500">
          <Spinner className="h-6 w-6" />
        </div>
      ) : projects.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-slate-700 p-12 text-center text-slate-400">
          <p>{emptyHint.title}</p>
          <p className="mt-1 text-sm">{emptyHint.body}</p>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          {projects.map((p) => (
            <div
              key={p.id}
              className={`group flex flex-col rounded-2xl border border-slate-800 bg-slate-900 p-5 transition-colors hover:border-slate-700 ${
                p.archived ? "opacity-70" : ""
              }`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                  <Link
                    to={`/projects/${p.id}`}
                    className="truncate text-lg font-medium text-slate-100 hover:text-cyan-400"
                  >
                    {p.name}
                  </Link>
                  {p.archived && (
                    <span className="shrink-0 rounded bg-amber-500/15 px-1.5 py-0.5 text-xs font-medium text-amber-300">
                      {t("projects.archived")}
                    </span>
                  )}
                  <HeartbeatChip p={p} />
                </div>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => toggleArchive(p)}
                    title={p.archived ? t("projects.unarchive") : t("projects.archive")}
                    aria-label={p.archived ? t("projects.unarchive") : t("projects.archive")}
                    className="rounded-lg px-2 py-1 text-slate-600 opacity-0 transition-opacity hover:bg-slate-800 hover:text-cyan-400 group-hover:opacity-100"
                  >
                    {p.archived ? "↩" : "📦"}
                  </button>
                  <button
                    onClick={() => remove(p)}
                    title="Remove project"
                    aria-label="Remove project"
                    className="rounded-lg px-2 py-1 text-slate-600 opacity-0 transition-opacity hover:bg-slate-800 hover:text-red-400 group-hover:opacity-100"
                  >
                    🗑
                  </button>
                </div>
              </div>
              {p.description && (
                <p className="mt-1 line-clamp-2 text-sm text-slate-400">{p.description}</p>
              )}
              <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
                {p.github_full_name && (
                  <a
                    href={p.github_url}
                    target="_blank"
                    rel="noreferrer"
                    onClick={(e) => e.stopPropagation()}
                    className="text-slate-400 hover:text-cyan-400"
                  >
                    {p.github_full_name}
                  </a>
                )}
                <span>•</span>
                {p.archived && p.archived_at ? (
                  <span title={`Archived: ${formatDate(p.archived_at)}`}>
                    Archived {formatDate(p.archived_at)}
                  </span>
                ) : (
                  <span>{formatDate(p.updated_at)}</span>
                )}
              </div>
              <div className="mt-4">
                <Button variant="subtle" onClick={() => navigate(`/projects/${p.id}`)}>
                  {t("projects.open")} →
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      {showModal && (
        <NewProjectModal
          onClose={() => setShowModal(false)}
          onCreated={(p) => {
            setShowModal(false);
            navigate(`/projects/${p.id}`);
          }}
        />
      )}

      {showSync && (
        <SyncFromGithubModal
          onClose={() => setShowSync(false)}
          onSynced={() => void load()}
        />
      )}
    </div>
  );
}

/** Compact badge on each project card summarising the heartbeat state.
 *  Visible only when there is something to say (i.e. enabled and has a
 *  GitHub repo, or recently errored). Tap = nothing — full controls live
 *  on the /heartbeat page (linked from the global nav). */
function HeartbeatChip({ p }: { p: Project }) {
  const { t } = useTranslation();
  if (p.archived) return null;
  if (!p.heartbeat_enabled) {
    return (
      <span
        className="shrink-0 rounded bg-slate-800 px-1.5 py-0.5 text-xs font-medium text-slate-500"
        title={t("projects.heartbeatOffTooltip")}
      >
        🤖 off
      </span>
    );
  }
  if (!p.github_full_name) return null;
  if (p.last_heartbeat_status === "error") {
    return (
      <span
        className="shrink-0 rounded bg-red-500/15 px-1.5 py-0.5 text-xs font-medium text-red-300"
        title={p.last_heartbeat_error || "Heartbeat error"}
      >
        🤖 {t("heartbeat.error")}
      </span>
    );
  }
  const tone =
    p.last_heartbeat_status === "cooldown"
      ? "bg-amber-500/15 text-amber-300"
      : p.last_heartbeat_status === "success" || p.last_heartbeat_status === "no_issues"
        ? "bg-emerald-500/15 text-emerald-300"
        : "bg-cyan-500/15 text-cyan-300";
  const lastTick = p.last_heartbeat_at
    ? relativeTime(parseApiDate(p.last_heartbeat_at))
    : "never";
  return (
    <span
      className={`shrink-0 rounded px-1.5 py-0.5 text-xs font-medium ${tone}`}
      title={t("projects.heartbeatActiveTooltip", { time: lastTick })}
    >
      🤖 {lastTick}
    </span>
  );
}

function relativeTime(d: Date): string {
  const diff = Date.now() - d.getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)} min ago`;
  if (diff < 86_400_000) return `${Math.round(diff / 3_600_000)} h ago`;
  return formatDate(d.toISOString());
}
