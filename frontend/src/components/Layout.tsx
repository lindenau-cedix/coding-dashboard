import { useTranslation } from "react-i18next";
import { Link, Outlet } from "react-router-dom";
import { useAuth } from "../auth";
import {
  LANGUAGE_LABELS,
  SUPPORTED_LANGUAGES,
  type SupportedLanguage,
  setLanguage,
} from "../i18n";

export default function Layout() {
  const { t, i18n } = useTranslation();
  const { username, logout, authRequired } = useAuth();
  const currentLang = (i18n.language?.split("-")[0] || "en") as SupportedLanguage;

  return (
    <div className="flex min-h-full flex-col">
      <header className="safe-top sticky top-0 z-10 border-b border-slate-800 bg-slate-900/80 backdrop-blur">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
          <div className="flex items-center gap-5">
            <Link to="/" className="flex items-center gap-2 font-semibold text-slate-100">
              <span className="text-cyan-400">⌘</span>
              Coding Dashboard
            </Link>
            <nav className="hidden items-center gap-1 text-sm sm:flex">
              <Link
                to="/"
                className="rounded-lg px-3 py-1.5 text-slate-300 hover:bg-slate-800 hover:text-slate-100"
              >
                {t("common.navProjects")}
              </Link>
              <Link
                to="/heartbeat"
                className="rounded-lg px-3 py-1.5 text-slate-300 hover:bg-slate-800 hover:text-slate-100"
              >
                {t("common.navHeartbeat")}
              </Link>
              <Link
                to="/settings/env-profiles"
                className="rounded-lg px-3 py-1.5 text-slate-300 hover:bg-slate-800 hover:text-slate-100"
              >
                {t("common.navSettings")}
              </Link>
            </nav>
          </div>
          <div className="flex items-center gap-3 text-sm text-slate-400">
            <label className="hidden items-center gap-1 text-xs text-slate-500 sm:flex">
              <span className="sr-only">{t("common.language")}</span>
              <select
                value={currentLang}
                onChange={(e) => setLanguage(e.target.value as SupportedLanguage)}
                className="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-200 outline-none focus:border-cyan-500"
                aria-label={t("common.language")}
              >
                {SUPPORTED_LANGUAGES.map((code) => (
                  <option key={code} value={code}>
                    {LANGUAGE_LABELS[code]}
                  </option>
                ))}
              </select>
            </label>
            {username && <span className="hidden sm:inline">{username}</span>}
            {authRequired && (
              <button
                onClick={logout}
                className="rounded-lg border border-slate-700 px-3 py-1.5 text-slate-200 hover:bg-slate-800"
              >
                {t("common.logout")}
              </button>
            )}
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}
