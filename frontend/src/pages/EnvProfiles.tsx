import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, ApiError } from "../api";
import { Button, ErrorText, Spinner } from "../components/ui";
import type { EnvProfile } from "../types";

const KEY_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;

interface DraftProfile {
  key: string;
  name: string;
  baseUrl: string;
  /** Plaintext token the operator just typed. Empty when not changing. */
  token: string;
}

const EMPTY_DRAFT: DraftProfile = {
  key: "",
  name: "",
  baseUrl: "",
  token: "",
};

export default function EnvProfiles() {
  const { t } = useTranslation();
  const [profiles, setProfiles] = useState<EnvProfile[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<DraftProfile>(EMPTY_DRAFT);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Pre-flight check: does the dashboard's CD_SECRET_KEY still look like
  // the bundled default? When true, the form refuses to save (the server
  // would 503 anyway) and a banner points the operator at install.sh.
  const [encryptionAvailable, setEncryptionAvailable] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const rows = await api.listEnvProfiles();
      setProfiles(rows);
      setError(null);
    } catch (e) {
      setError((e as Error).message ?? String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const sorted = useMemo(
    () => [...profiles].sort((a, b) => a.key.localeCompare(b.key)),
    [profiles],
  );

  function startCreate() {
    setEditing(null);
    setDraft(EMPTY_DRAFT);
    setError(null);
  }

  function startEdit(p: EnvProfile) {
    setEditing(p.key);
    // Pre-fill the form with the profile's existing values. ``token`` stays
    // empty — the GET response never carries plaintext, so PATCH with
    // empty means "leave unchanged".
    setDraft({
      key: p.key,
      name: p.name,
      baseUrl: p.anthropic_base_url,
      token: "",
    });
    setError(null);
  }

  function validate(d: DraftProfile): string | null {
    if (!KEY_RE.test(d.key)) {
      return t("envProfiles.validateKey");
    }
    if (!d.name.trim()) return t("envProfiles.validateName");
    if (d.baseUrl && !/^https?:\/\//.test(d.baseUrl)) {
      return t("envProfiles.validateBaseUrl");
    }
    // Token is OPTIONAL on POST (token-only is the only writable field).
    // On PATCH empty == "leave unchanged" so we never need to validate it.
    return null;
  }

  async function save() {
    setError(null);
    const validationError = validate(draft);
    if (validationError) {
      setError(validationError);
      return;
    }
    setBusy(true);
    try {
      if (editing) {
        // PATCH: empty token means "leave the stored token alone".
        // We pass the full draft as the schema requires.
        const payload = {
          key: draft.key,
          name: draft.name,
          anthropic_base_url: draft.baseUrl,
          // Only send the plaintext token when the user actually typed
          // something. Empty string would 422 ("leave alone" is the
          // convention we encode by omitting the field).
          ...(draft.token ? { anthropic_auth_token: draft.token } : {}),
        };
        await api.updateEnvProfile(editing, payload as DraftProfile & {
          anthropic_base_url?: string;
          anthropic_auth_token?: string;
        });
      } else {
        // POST: token may be empty (operator may only want to redirect
        // the base URL). The server refuses a non-empty token when
        // encryption is unavailable (503).
        if (draft.token && !encryptionAvailable) {
          setError(t("envProfiles.encryptionWarningShort"));
          return;
        }
        await api.createEnvProfile({
          key: draft.key,
          name: draft.name,
          anthropic_base_url: draft.baseUrl,
          anthropic_auth_token: draft.token,
        });
      }
      setDraft(EMPTY_DRAFT);
      setEditing(null);
      await refresh();
    } catch (e) {
      if (e instanceof ApiError && e.status === 503) {
        setEncryptionAvailable(false);
      }
      setError((e as Error).message ?? String(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(key: string) {
    if (!confirm(t("envProfiles.deleteConfirm", { name: key }))) return;
    setBusy(true);
    try {
      await api.deleteEnvProfile(key);
      if (editing === key) {
        setEditing(null);
        setDraft(EMPTY_DRAFT);
      }
      await refresh();
    } catch (e) {
      setError((e as Error).message ?? String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold text-slate-100">
          {t("envProfiles.pageTitle")}
        </h1>
        <p className="mt-1 text-sm text-slate-400">
          {t("envProfiles.pageIntro")}
        </p>
        {!encryptionAvailable && (
          <p className="mt-3 rounded-lg border border-amber-700 bg-amber-900/40 p-3 text-sm text-amber-200">
            {t("envProfiles.encryptionWarning")}
          </p>
        )}
      </header>

      <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-4">
        <h2 className="text-base font-medium text-slate-200">
          {editing
            ? t("envProfiles.editTitle", { name: editing })
            : t("envProfiles.newTitle")}
        </h2>
        <form
          className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <label className="block text-sm">
            <span className="text-slate-300">{t("envProfiles.key")}</span>
            <input
              type="text"
              className="mt-1 block w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-slate-100 outline-none focus:border-cyan-500"
              value={draft.key}
              onChange={(e) => setDraft({ ...draft, key: e.target.value })}
              disabled={!!editing || busy}
              pattern="[a-z0-9][a-z0-9-]{0,62}"
              required
            />
            <span className="mt-1 block text-xs text-slate-500">
              {t("envProfiles.keyHint")}
            </span>
          </label>
          <label className="block text-sm">
            <span className="text-slate-300">{t("envProfiles.name")}</span>
            <input
              type="text"
              className="mt-1 block w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-slate-100 outline-none focus:border-cyan-500"
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              maxLength={200}
              required
            />
          </label>
          <label className="block text-sm sm:col-span-2">
            <span className="text-slate-300">{t("envProfiles.baseUrlLabel")}</span>
            <input
              type="url"
              className="mt-1 block w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-slate-100 outline-none focus:border-cyan-500"
              value={draft.baseUrl}
              onChange={(e) =>
                setDraft({ ...draft, baseUrl: e.target.value })
              }
              placeholder={t("envProfiles.baseUrlPlaceholder")}
            />
          </label>
          <label className="block text-sm sm:col-span-2">
            <span className="text-slate-300">
              {t("envProfiles.tokenLabel")}{" "}
              {editing && draft.token === "" ? t("envProfiles.tokenUnchanged") : ""}
            </span>
            <input
              type="password"
              autoComplete="off"
              className="mt-1 block w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-slate-100 outline-none focus:border-cyan-500"
              value={draft.token}
              onChange={(e) => setDraft({ ...draft, token: e.target.value })}
              placeholder={
                editing
                  ? t("envProfiles.tokenUnchangedPlaceholder")
                  : t("envProfiles.tokenBaseUrlOnlyPlaceholder")
              }
            />
            <span className="mt-1 block text-xs text-amber-300/80">
              {t("envProfiles.tokenSaveHint")}
            </span>
          </label>

          <div className="mt-2 flex flex-wrap items-center gap-2 sm:col-span-2">
            <Button type="submit" disabled={busy}>
              {busy
                ? t("envProfiles.saving")
                : editing
                ? t("envProfiles.saveChanges")
                : t("envProfiles.createSubmit")}
            </Button>
            <button
              type="button"
              onClick={startCreate}
              className="text-sm text-slate-300 underline-offset-2 hover:underline"
            >
              {t("envProfiles.resetForm")}
            </button>
          </div>
        </form>
        {error && <ErrorText className="mt-3">{error}</ErrorText>}
      </section>

      <section>
        <h2 className="text-base font-medium text-slate-200">
          {t("envProfiles.existingCount", { count: sorted.length })}
        </h2>
        {sorted.length === 0 ? (
          <p className="mt-2 text-sm text-slate-400">
            {t("envProfiles.noneYet")}
          </p>
        ) : (
          <ul className="mt-3 divide-y divide-slate-800 rounded-xl border border-slate-800 bg-slate-900/40">
            {sorted.map((p) => (
              <li
                key={p.key}
                className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 text-sm"
              >
                <div className="min-w-0">
                  <div className="font-medium text-slate-100">{p.name}</div>
                  <div className="text-xs text-slate-400">
                    key=<code className="font-mono">{p.key}</code>
                    {p.anthropic_base_url && (
                      <>
                        {" · "}
                        base_url=<code className="font-mono">
                          {p.anthropic_base_url}
                        </code>
                      </>
                    )}
                  </div>
                  <div className="text-xs">
                    {p.anthropic_auth_token_set ? (
                      <span className="text-emerald-400">
                        {t("envProfiles.tokenSetWithHint", {
                          hint: p.anthropic_auth_token_hint,
                        })}
                      </span>
                    ) : (
                      <span className="text-slate-500">
                        {t("envProfiles.noToken")}
                      </span>
                    )}
                  </div>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() => startEdit(p)}
                    className="rounded-lg border border-slate-700 px-3 py-1.5 text-slate-200 hover:bg-slate-800"
                  >
                    {t("envProfiles.edit")}
                  </button>
                  <button
                    onClick={() => void remove(p.key)}
                    className="rounded-lg border border-rose-700 px-3 py-1.5 text-rose-200 hover:bg-rose-900/40"
                  >
                    {t("envProfiles.delete")}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
