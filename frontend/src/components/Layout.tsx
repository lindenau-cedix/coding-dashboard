import { Link, Outlet } from "react-router-dom";
import { useAuth } from "../auth";

export default function Layout() {
  const { username, logout, authRequired } = useAuth();
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
                Projekte
              </Link>
              <Link
                to="/heartbeat"
                className="rounded-lg px-3 py-1.5 text-slate-300 hover:bg-slate-800 hover:text-slate-100"
              >
                🤖 Heartbeat
              </Link>
            </nav>
          </div>
          <div className="flex items-center gap-3 text-sm text-slate-400">
            {username && <span className="hidden sm:inline">{username}</span>}
            {authRequired && (
              <button
                onClick={logout}
                className="rounded-lg border border-slate-700 px-3 py-1.5 text-slate-200 hover:bg-slate-800"
              >
                Logout
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
