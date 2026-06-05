import type { ButtonHTMLAttributes, ReactNode } from "react";
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

export function ErrorText({ children }: { children: ReactNode }) {
  if (!children) return null;
  return (
    <p className="rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
      {children}
    </p>
  );
}

export function formatDate(iso: string | null): string {
  if (!iso) return "–";
  const d = new Date(iso);
  return d.toLocaleString("de-DE", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}
