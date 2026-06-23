import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { GithubRepo } from "../types";
import { Button, ErrorText, Modal, Spinner } from "./ui";

type Phase =
  | { kind: "loading" }
  | { kind: "preview"; repos: GithubRepo[]; user: string; error?: string }
  | { kind: "syncing"; selected: string[] }
  | { kind: "result"; summary: { imported: number; skipped: number; failed: number }; results: { full_name: string; status: "imported" | "skipped" | "failed"; detail: string }[] };

/** "Sync from GitHub" dialog: lists every repo visible to the token and
 *  bulk-clones the ones the user picks (or every not-yet-imported one if
 *  they hit "Sync all"). */
export default function SyncFromGithubModal({
  onClose,
  onSynced,
}: {
  onClose: () => void;
  onSynced: () => void;
}) {
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  const [filter, setFilter] = useState("");
  const [includeForks, setIncludeForks] = useState(true);
  const [includeArchived, setIncludeArchived] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const { repos, user } = await api.listFromGithub();
        if (!active) return;
        setPhase({ kind: "preview", repos, user });
        // Preselect everything not already imported.
        setSelected(new Set(repos.filter((r) => !r.already_imported).map((r) => r.full_name)));
      } catch (err) {
        if (!active) return;
        const message = err instanceof Error ? err.message : "Laden fehlgeschlagen";
        setPhase({ kind: "preview", repos: [], user: "", error: message });
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  const filtered = useMemo(() => {
    if (phase.kind !== "preview") return [];
    const q = filter.trim().toLowerCase();
    return phase.repos.filter((r) => {
      if (!includeForks && r.fork) return false;
      if (!includeArchived && r.archived) return false;
      if (!q) return true;
      return (
        r.full_name.toLowerCase().includes(q) ||
        r.name.toLowerCase().includes(q) ||
        (r.description || "").toLowerCase().includes(q)
      );
    });
  }, [phase, filter, includeForks, includeArchived]);

  const selectedCount = useMemo(
    () => (phase.kind === "preview" ? phase.repos.filter((r) => selected.has(r.full_name)).length : 0),
    [phase, selected],
  );

  function toggle(fullName: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(fullName)) next.delete(fullName);
      else next.add(fullName);
      return next;
    });
  }

  async function sync() {
    if (phase.kind !== "preview") return;
    const names = phase.repos.filter((r) => selected.has(r.full_name)).map((r) => r.full_name);
    if (!names.length) return;
    setPhase({ kind: "syncing", selected: names });
    try {
      const res = await api.syncFromGithub({
        full_names: names,
        include_forks: includeForks,
        include_archived: includeArchived,
      });
      setPhase({
        kind: "result",
        summary: { imported: res.imported, skipped: res.skipped, failed: res.failed },
        results: res.results.map((r) => ({
          full_name: r.full_name,
          status: r.status,
          detail: r.detail,
        })),
      });
      if (res.imported > 0) onSynced();
    } catch (err) {
      const message = err instanceof Error ? err.message : "Sync fehlgeschlagen";
      // Return to preview with the error so the user can retry.
      setPhase({ kind: "preview", repos: phase.repos, user: phase.user, error: message });
    }
  }

  return (
    <Modal title="Repos von GitHub synchronisieren" onClose={onClose}>
      {phase.kind === "loading" && (
        <div className="flex items-center gap-2 py-8 text-slate-400">
          <Spinner className="h-5 w-5" /> Lade Repos von GitHub…
        </div>
      )}

      {phase.kind === "syncing" && (
        <div className="flex items-center gap-2 py-8 text-slate-400">
          <Spinner className="h-5 w-5" /> Klone {phase.selected.length} Repos…
        </div>
      )}

      {phase.kind === "preview" && (
        <div className="space-y-3">
          {phase.error && <ErrorText>{phase.error}</ErrorText>}
          {phase.user && (
            <p className="text-xs text-slate-500">
              Sichtbar für <span className="font-mono text-slate-300">{phase.user}</span>: {phase.repos.length} Repos
              ({phase.repos.filter((r) => !r.already_imported).length} noch nicht importiert).
            </p>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <input
              type="search"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filtern…"
              className="flex-1 rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-cyan-500"
            />
            <label className="flex items-center gap-1 text-xs text-slate-400">
              <input
                type="checkbox"
                checked={includeForks}
                onChange={(e) => setIncludeForks(e.target.checked)}
                className="h-3.5 w-3.5 accent-cyan-500"
              />
              Forks
            </label>
            <label className="flex items-center gap-1 text-xs text-slate-400">
              <input
                type="checkbox"
                checked={includeArchived}
                onChange={(e) => setIncludeArchived(e.target.checked)}
                className="h-3.5 w-3.5 accent-cyan-500"
              />
              Archiviert
            </label>
          </div>
          <div className="max-h-80 overflow-y-auto rounded-lg border border-slate-800">
            {filtered.length === 0 ? (
              <p className="p-4 text-sm text-slate-500">
                {phase.repos.length === 0
                  ? "Keine Repos gefunden. Prüfe den GitHub-Token und seine Berechtigungen."
                  : "Keine Repos passen zum Filter."}
              </p>
            ) : (
              <ul className="divide-y divide-slate-800">
                {filtered.map((r) => (
                  <li
                    key={r.full_name}
                    className="flex items-start gap-2 px-3 py-2 text-sm hover:bg-slate-800/40"
                  >
                    <input
                      type="checkbox"
                      checked={selected.has(r.full_name)}
                      onChange={() => toggle(r.full_name)}
                      disabled={r.already_imported}
                      className="mt-1 h-4 w-4 accent-cyan-500"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                        <span
                          className={`truncate font-mono ${r.already_imported ? "text-slate-500" : "text-slate-200"}`}
                        >
                          {r.full_name}
                        </span>
                        {r.private && (
                          <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
                            privat
                          </span>
                        )}
                        {r.fork && (
                          <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
                            fork
                          </span>
                        )}
                        {r.archived && (
                          <span className="rounded bg-amber-900/40 px-1.5 py-0.5 text-[10px] text-amber-300">
                            archiviert
                          </span>
                        )}
                        {r.already_imported && (
                          <span className="rounded bg-emerald-900/40 px-1.5 py-0.5 text-[10px] text-emerald-300">
                            bereits importiert
                          </span>
                        )}
                      </div>
                      {r.description && (
                        <p className="mt-0.5 line-clamp-1 text-xs text-slate-500">{r.description}</p>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="flex items-center justify-between gap-2 pt-2">
            <span className="text-xs text-slate-500">{selectedCount} ausgewählt</span>
            <div className="flex gap-2">
              <Button variant="ghost" onClick={onClose}>
                Abbrechen
              </Button>
              <Button onClick={sync} disabled={selectedCount === 0}>
                {selectedCount === 0
                  ? "Nichts ausgewählt"
                  : selectedCount === phase.repos.length
                    ? `Alle ${selectedCount} klonen`
                    : `${selectedCount} klonen`}
              </Button>
            </div>
          </div>
        </div>
      )}

      {phase.kind === "result" && (
        <div className="space-y-3">
          <p className="text-sm text-slate-300">
            <span className="text-emerald-400">{phase.summary.imported} importiert</span>
            {phase.summary.skipped > 0 && (
              <>, <span className="text-slate-400">{phase.summary.skipped} übersprungen</span></>
            )}
            {phase.summary.failed > 0 && (
              <>, <span className="text-red-400">{phase.summary.failed} fehlgeschlagen</span></>
            )}
            .
          </p>
          <div className="max-h-72 overflow-y-auto rounded-lg border border-slate-800">
            <ul className="divide-y divide-slate-800 font-mono text-xs">
              {phase.results.map((r) => (
                <li
                  key={r.full_name}
                  className="flex items-baseline gap-2 px-3 py-1.5"
                >
                  <span
                    className={
                      r.status === "imported"
                        ? "text-emerald-400"
                        : r.status === "failed"
                          ? "text-red-400"
                          : "text-slate-500"
                    }
                  >
                    {r.status === "imported" ? "✓" : r.status === "failed" ? "✗" : "·"}
                  </span>
                  <span className="text-slate-300">{r.full_name}</span>
                  {r.detail && <span className="truncate text-slate-500">— {r.detail}</span>}
                </li>
              ))}
            </ul>
          </div>
          <div className="flex justify-end pt-1">
            <Button onClick={onClose}>Schließen</Button>
          </div>
        </div>
      )}
    </Modal>
  );
}
