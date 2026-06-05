import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import NewProjectModal from "../components/NewProjectModal";
import { Button, ErrorText, Spinner, formatDate } from "../components/ui";
import type { Project } from "../types";

export default function Projects() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState("");
  const [showModal, setShowModal] = useState(false);

  async function load() {
    try {
      setProjects(await api.listProjects());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Laden fehlgeschlagen");
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function remove(p: Project) {
    if (!confirm(`Projekt "${p.name}" lokal entfernen?\n(Das GitHub-Repository bleibt bestehen.)`))
      return;
    try {
      await api.deleteProject(p.id, false);
      setProjects((prev) => prev?.filter((x) => x.id !== p.id) ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Löschen fehlgeschlagen");
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-slate-100">Projekte</h1>
        <Button onClick={() => setShowModal(true)}>+ Neues Projekt</Button>
      </div>

      <ErrorText>{error}</ErrorText>

      {projects === null ? (
        <div className="flex justify-center py-16 text-slate-500">
          <Spinner className="h-6 w-6" />
        </div>
      ) : projects.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-slate-700 p-12 text-center text-slate-400">
          <p>Noch keine Projekte.</p>
          <p className="mt-1 text-sm">
            Lege ein neues an oder importiere ein bestehendes GitHub-Repo.
          </p>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          {projects.map((p) => (
            <div
              key={p.id}
              className="group flex flex-col rounded-2xl border border-slate-800 bg-slate-900 p-5 transition-colors hover:border-slate-700"
            >
              <div className="flex items-start justify-between gap-2">
                <Link
                  to={`/projects/${p.id}`}
                  className="text-lg font-medium text-slate-100 hover:text-cyan-400"
                >
                  {p.name}
                </Link>
                <button
                  onClick={() => remove(p)}
                  title="Projekt entfernen"
                  className="rounded-lg px-2 py-1 text-slate-600 opacity-0 transition-opacity hover:bg-slate-800 hover:text-red-400 group-hover:opacity-100"
                >
                  🗑
                </button>
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
                <span>{formatDate(p.updated_at)}</span>
              </div>
              <div className="mt-4">
                <Button variant="subtle" onClick={() => navigate(`/projects/${p.id}`)}>
                  Öffnen →
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
    </div>
  );
}
