import { useState } from "react";
import { api } from "../api";
import type { Project } from "../types";
import { Button, ErrorText, Modal } from "./ui";

export default function NewProjectModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (p: Project) => void;
}) {
  const [mode, setMode] = useState<"create" | "import">("create");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [isPrivate, setIsPrivate] = useState(true);
  const [repo, setRepo] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!name.trim()) {
      setError("Bitte einen Namen angeben.");
      return;
    }
    if (mode === "import" && !repo.trim()) {
      setError("Bitte 'owner/repo' oder eine GitHub-URL angeben.");
      return;
    }
    setBusy(true);
    try {
      const project = await api.createProject({
        name: name.trim(),
        description: description.trim(),
        mode,
        private: isPrivate,
        repo: repo.trim(),
      });
      onCreated(project);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Anlegen fehlgeschlagen");
    } finally {
      setBusy(false);
    }
  }

  const tab = (m: "create" | "import", label: string) => (
    <button
      type="button"
      onClick={() => setMode(m)}
      className={`flex-1 rounded-lg px-3 py-2 text-sm transition-colors ${
        mode === m ? "bg-cyan-500 text-slate-900" : "bg-slate-800 text-slate-300 hover:bg-slate-700"
      }`}
    >
      {label}
    </button>
  );

  return (
    <Modal title="Neues Projekt" onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <div className="flex gap-2">
          {tab("create", "Neu erstellen")}
          {tab("import", "Importieren")}
        </div>

        <ErrorText>{error}</ErrorText>

        <div className="space-y-1">
          <label className="text-sm text-slate-300">Name</label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Mein Projekt"
            className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-slate-100 outline-none focus:border-cyan-500"
          />
        </div>

        {mode === "create" ? (
          <>
            <div className="space-y-1">
              <label className="text-sm text-slate-300">Beschreibung (optional)</label>
              <input
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-slate-100 outline-none focus:border-cyan-500"
              />
            </div>
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={isPrivate}
                onChange={(e) => setIsPrivate(e.target.checked)}
                className="h-4 w-4 accent-cyan-500"
              />
              Privates Repository
            </label>
            <p className="text-xs text-slate-500">
              Erstellt ein neues GitHub-Repository und klont es auf den Server.
            </p>
          </>
        ) : (
          <div className="space-y-1">
            <label className="text-sm text-slate-300">Repository</label>
            <input
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder="owner/repo oder https://github.com/owner/repo"
              className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-slate-100 outline-none focus:border-cyan-500"
            />
            <p className="text-xs text-slate-500">
              Importiert ein bestehendes Repository (muss für den Token zugänglich sein).
            </p>
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Abbrechen
          </Button>
          <Button type="submit" disabled={busy}>
            {busy ? "Wird angelegt…" : mode === "create" ? "Erstellen" : "Importieren"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
