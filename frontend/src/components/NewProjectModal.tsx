import { useState } from "react";
import { useTranslation } from "react-i18next";
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
  const { t } = useTranslation();
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
      setError(t("newProject.errorName"));
      return;
    }
    if (mode === "import" && !repo.trim()) {
      setError(t("newProject.errorRepo"));
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
      setError(err instanceof Error ? err.message : t("newProject.createFailed"));
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
    <Modal title={t("newProject.title")} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <div className="flex gap-2">
          {tab("create", t("newProject.tabCreate"))}
          {tab("import", t("newProject.tabImport"))}
        </div>

        <ErrorText>{error}</ErrorText>

        <div className="space-y-1">
          <label className="text-sm text-slate-300">{t("newProject.name")}</label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("newProject.namePlaceholder")}
            className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-slate-100 outline-none focus:border-cyan-500"
          />
        </div>

        {mode === "create" ? (
          <>
            <div className="space-y-1">
              <label className="text-sm text-slate-300">{t("newProject.description")}</label>
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
              {t("newProject.private")}
            </label>
            <p className="text-xs text-slate-500">{t("newProject.createHint")}</p>
          </>
        ) : (
          <div className="space-y-1">
            <label className="text-sm text-slate-300">{t("newProject.repository")}</label>
            <input
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder={t("newProject.repositoryPlaceholder")}
              className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-slate-100 outline-none focus:border-cyan-500"
            />
            <p className="text-xs text-slate-500">{t("newProject.importHint")}</p>
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("newProject.cancel")}
          </Button>
          <Button type="submit" disabled={busy}>
            {busy
              ? t("newProject.submitting")
              : mode === "create"
                ? t("newProject.create")
                : t("newProject.import")}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
