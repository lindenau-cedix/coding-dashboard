import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { DirListing, FileContent, FileEntry } from "../types";
import { FullscreenShell, IconButton, Spinner } from "./ui";

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function fileIcon(entry: FileEntry): string {
  return entry.is_dir ? "📁" : "📄";
}

/** A small file browser for a project's working tree with a side-by-side viewer. */
export default function FileBrowser({ projectId }: { projectId: string }) {
  const [path, setPath] = useState("");
  const [listing, setListing] = useState<DirListing | null>(null);
  const [loadingDir, setLoadingDir] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [file, setFile] = useState<FileContent | null>(null);
  const [loadingFile, setLoadingFile] = useState(false);
  const [error, setError] = useState("");
  const [fsFile, setFsFile] = useState(false);
  const reqId = useRef(0);

  async function loadDir(p: string) {
    setLoadingDir(true);
    setError("");
    try {
      const res = await api.listFiles(projectId, p);
      setListing(res);
      setPath(res.path);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Verzeichnis konnte nicht geladen werden");
    } finally {
      setLoadingDir(false);
    }
  }

  useEffect(() => {
    void loadDir("");
    setSelected(null);
    setFile(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  async function openFile(entry: FileEntry) {
    if (entry.is_dir) {
      void loadDir(entry.path);
      return;
    }
    setSelected(entry.path);
    setLoadingFile(true);
    setError("");
    const id = ++reqId.current;
    try {
      const res = await api.readFile(projectId, entry.path);
      if (id === reqId.current) setFile(res);
    } catch (e) {
      if (id === reqId.current) {
        setFile(null);
        setError(e instanceof Error ? e.message : "Datei konnte nicht geladen werden");
      }
    } finally {
      if (id === reqId.current) setLoadingFile(false);
    }
  }

  const segments = path ? path.split("/") : [];

  const breadcrumb = (
    <div className="flex flex-wrap items-center gap-1 text-xs text-slate-400">
      <button
        onClick={() => void loadDir("")}
        className={`rounded px-1.5 py-0.5 hover:bg-slate-800 hover:text-cyan-300 ${path === "" ? "text-slate-200" : ""}`}
      >
        root
      </button>
      {segments.map((seg, i) => {
        const sub = segments.slice(0, i + 1).join("/");
        return (
          <span key={sub} className="flex items-center gap-1">
            <span className="text-slate-600">/</span>
            <button
              onClick={() => void loadDir(sub)}
              className={`rounded px-1.5 py-0.5 hover:bg-slate-800 hover:text-cyan-300 ${i === segments.length - 1 ? "text-slate-200" : ""}`}
            >
              {seg}
            </button>
          </span>
        );
      })}
    </div>
  );

  const fileList = (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center justify-between gap-2 border-b border-slate-800 px-3 py-2">
        {breadcrumb}
        <IconButton label="Aktualisieren" onClick={() => void loadDir(path)}>
          {loadingDir ? <Spinner className="h-3.5 w-3.5" /> : "⟳"}
        </IconButton>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {path !== "" && (
          <button
            onClick={() => void loadDir(segments.slice(0, -1).join("/"))}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm text-slate-400 hover:bg-slate-800"
          >
            <span>↩</span> ..
          </button>
        )}
        {listing?.entries.length === 0 && (
          <p className="px-3 py-2 text-sm text-slate-600">Leeres Verzeichnis.</p>
        )}
        {listing?.entries.map((entry) => (
          <button
            key={entry.path}
            onClick={() => void openFile(entry)}
            className={`flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-sm transition hover:bg-slate-800 ${
              selected === entry.path ? "bg-slate-800 text-cyan-300" : "text-slate-300"
            }`}
          >
            <span className="flex min-w-0 items-center gap-2">
              <span>{fileIcon(entry)}</span>
              <span className="truncate">{entry.name}</span>
            </span>
            {!entry.is_dir && (
              <span className="shrink-0 text-xs text-slate-600">{fmtSize(entry.size)}</span>
            )}
          </button>
        ))}
      </div>
    </div>
  );

  const viewerBody = (large = false) => {
    if (!selected) {
      return (
        <div className="flex h-full items-center justify-center text-sm text-slate-600">
          Datei auswählen, um den Inhalt anzuzeigen.
        </div>
      );
    }
    if (loadingFile) {
      return (
        <div className="flex h-full items-center justify-center text-slate-500">
          <Spinner className="h-5 w-5" />
        </div>
      );
    }
    if (!file) {
      return (
        <div className="flex h-full items-center justify-center text-sm text-slate-600">
          (kein Inhalt)
        </div>
      );
    }
    if (file.is_binary) {
      return (
        <div className="flex h-full items-center justify-center text-sm text-slate-500">
          Binärdatei – {fmtSize(file.size)}
        </div>
      );
    }
    return (
      <pre
        className={`min-h-0 flex-1 overflow-auto rounded-lg bg-slate-950 p-3 font-mono ${large ? "text-sm" : "text-xs"} leading-relaxed text-slate-300`}
      >
        {file.content}
        {file.truncated && (
          <span className="mt-2 block text-amber-400">[… Datei gekürzt – nur erste 512 KB …]</span>
        )}
      </pre>
    );
  };

  const viewer = (
    <div className="flex min-h-0 flex-1 flex-col border-t border-slate-800 sm:border-t-0 sm:border-l">
      <div className="flex items-center justify-between gap-2 border-b border-slate-800 px-3 py-2">
        <span className="truncate font-mono text-xs text-slate-400">{selected ?? "—"}</span>
        {selected && file && !file.is_binary && (
          <IconButton label="Vollbild" onClick={() => setFsFile(true)}>
            ⛶
          </IconButton>
        )}
      </div>
      <div className="flex min-h-0 flex-1 flex-col p-2">{viewerBody(false)}</div>
    </div>
  );

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900">
      {error && (
        <p className="border-b border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
          {error}
        </p>
      )}
      <div className="flex h-[28rem] flex-col sm:flex-row">
        <div className="flex min-h-0 flex-1 sm:max-w-xs">{fileList}</div>
        {viewer}
      </div>

      {fsFile && file && selected && (
        <FullscreenShell title={<span className="font-mono">{selected}</span>} onClose={() => setFsFile(false)}>
          {viewerBody(true)}
        </FullscreenShell>
      )}
    </div>
  );
}
