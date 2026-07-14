import type { ButtonHTMLAttributes, ReactNode } from "react";
import { useEffect } from "react";
import { createPortal } from "react-dom";
import type { TaskStatus } from "../types";

type Variant = "primary" | "ghost" | "danger" | "subtle";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-cyan-500 hover:bg-cyan-400 text-slate-900 font-medium",
  ghost: "bg-transparent hover:bg-slate-800 text-slate-200 border border-slate-700",
  danger: "bg-red-600 hover:bg-red-500 text-white",
  subtle: "bg-slate-800 hover:bg-slate-700 text-slate-200",
};

export function Button({
  variant = "primary",
  className = "",
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  children: ReactNode;
}) {
  return (
    <button
      {...rest}
      className={`inline-flex items-center justify-center gap-2 rounded-lg px-3.5 py-2 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${VARIANTS[variant]} ${className}`}
    >
      {children}
    </button>
  );
}

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <span
      className={`inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent ${className}`}
    />
  );
}

const STATUS_STYLES: Record<string, string> = {
  queued: "bg-slate-700 text-slate-200",
  running: "bg-amber-500/20 text-amber-300 border border-amber-500/40",
  success: "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40",
  failed: "bg-red-500/20 text-red-300 border border-red-500/40",
  error: "bg-red-500/20 text-red-300 border border-red-500/40",
  interrupted: "bg-slate-600 text-slate-200",
  cancelled: "bg-slate-600 text-slate-200",
};

const STATUS_LABELS: Record<string, string> = {
  queued: "Wartet",
  running: "Läuft",
  success: "Erfolg",
  failed: "Fehlgeschlagen",
  error: "Fehler",
  interrupted: "Unterbrochen",
  cancelled: "Abgebrochen",
};

export function StatusBadge({ status }: { status: TaskStatus }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${
        STATUS_STYLES[status] ?? "bg-slate-700 text-slate-200"
      }`}
    >
      {status === "running" && <Spinner className="h-3 w-3" />}
      {STATUS_LABELS[status] ?? status}
    </span>
  );
}

export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="mt-12 w-full max-w-lg rounded-2xl border border-slate-700 bg-slate-900 p-6 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-slate-100">{title}</h2>
          <button
            onClick={onClose}
            className="rounded-lg px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-200"
          >
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

/** Small square icon button (e.g. the fullscreen toggle on consoles). */
export function IconButton({
  label,
  onClick,
  children,
  className = "",
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      className={`inline-flex h-7 w-7 items-center justify-center rounded-md border border-slate-700 bg-slate-800 text-slate-300 transition hover:bg-slate-700 hover:text-cyan-300 ${className}`}
    >
      {children}
    </button>
  );
}

/** Full-viewport overlay used to expand a console/terminal/output to fullscreen.
 *  Rendered via a portal so it escapes any clipping/scroll container.  Esc closes. */
export function FullscreenShell({
  title,
  onClose,
  children,
  headerRight,
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  headerRight?: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return createPortal(
    <div className="fixed inset-0 z-[60] flex flex-col bg-slate-950">
      <div className="flex items-center justify-between gap-3 border-b border-slate-800 px-4 py-2.5">
        <div className="min-w-0 truncate text-sm font-medium text-slate-200">{title}</div>
        <div className="flex items-center gap-2">
          {headerRight}
          <button
            onClick={onClose}
            className="rounded-lg border border-slate-700 px-3 py-1 text-sm text-slate-300 hover:bg-slate-800"
          >
            Schließen ✕
          </button>
        </div>
      </div>
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden p-4">{children}</div>
    </div>,
    document.body,
  );
}

export function ErrorText({
  className = "",
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  if (!children) return null;
  return (
    <p
      className={`rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300 ${className}`}
    >
      {children}
    </p>
  );
}

export function formatDate(iso: string | null): string {
  if (!iso) return "–";
  const d = parseApiDate(iso);
  return d.toLocaleString("de-DE", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

/** Parse a datetime string returned by the dashboard backend.
 *
 * The backend *should* always emit ISO-8601 with a UTC offset (``...Z`` or
 * ``...+00:00``) but the SQLite round-trip used to strip the offset, and
 * we want the UI to keep rendering correctly even if that regresses.
 * Naive ISO strings (no offset) are interpreted as UTC; everything else
 * falls back to the JS Date parser.
 */
export function parseApiDate(iso: string): Date {
  if (!/[Zz]|[+-]\d{2}:?\d{2}$/.test(iso)) {
    return new Date(iso + "Z");
  }
  return new Date(iso);
}
