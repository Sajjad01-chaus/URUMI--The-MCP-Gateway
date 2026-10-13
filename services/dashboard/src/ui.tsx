import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { ApiError } from "./api";

export function messageOf(error: unknown): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return "Something went wrong.";
}

/** Runs `load` on mount (and every `pollMs` if given); exposes data, error and a manual reload. */
export function useLoad<T>(load: () => Promise<T>, pollMs?: number) {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;

  const reload = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError(null);
    } catch (err) {
      setError(messageOf(err));
    }
  }, []);

  useEffect(() => {
    void reload();
    if (!pollMs) return;
    const timer = window.setInterval(() => void reload(), pollMs);
    return () => window.clearInterval(timer);
  }, [reload, pollMs]);

  return { data, error, reload, setData };
}

/** Wraps an async action with a busy flag and an error message for the UI. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(async <T,>(action: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true);
    setError(null);
    try {
      return await action();
    } catch (err) {
      setError(messageOf(err));
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, setError, run };
}

export function ErrorBanner({ message }: { message: string | null | undefined }) {
  if (!message) return null;
  return (
    <div className="banner banner-error" role="alert">
      {message}
    </div>
  );
}

export function Badge({ tone, children }: { tone: "green" | "red" | "amber" | "gray" | "blue" | "violet"; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function StatusBadge({ status }: { status: string }) {
  const tone = { connected: "green", unreachable: "red", disabled: "gray", unknown: "amber" }[status] ?? "gray";
  const label = { connected: "Connected", unreachable: "Unreachable", disabled: "Disabled", unknown: "Checking…" }[status] ?? status;
  return <Badge tone={tone as "green"}>{label}</Badge>;
}

export function RoleBadge({ role }: { role: string }) {
  return <Badge tone={role === "admin" ? "violet" : "blue"}>{role}</Badge>;
}

export function OutcomeBadge({ outcome }: { outcome: string }) {
  const tone = { success: "green", error: "red", timeout: "amber", denied: "violet", unavailable: "red" }[outcome] ?? "gray";
  return <Badge tone={tone as "green"}>{outcome}</Badge>;
}

/** Closes an overlay (modal, drawer) when Escape is pressed. */
export function useEscape(onClose: () => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
}

export function Modal({ title, onClose, children, wide = false }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  useEscape(onClose);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? "modal-wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-header">
          <h2>{title}</h2>
          <button className="icon-button" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function ConfirmDialog(props: {
  title: string;
  message: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => Promise<void>;
  onClose: () => void;
}) {
  const { busy, error, run } = useAction();
  return (
    <Modal title={props.title} onClose={props.onClose}>
      <p className="muted">{props.message}</p>
      <ErrorBanner message={error} />
      <div className="modal-actions">
        <button className="button" onClick={props.onClose} disabled={busy}>
          Cancel
        </button>
        <button
          className={`button ${props.danger ? "button-danger" : "button-primary"}`}
          disabled={busy}
          onClick={() => run(async () => {
            await props.onConfirm();
            props.onClose();
          })}
        >
          {busy ? "Working…" : props.confirmLabel}
        </button>
      </div>
    </Modal>
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      window.prompt("Copy this value:", text);
    }
  };
  return (
    <button className="button button-small" onClick={copy} type="button">
      {copied ? "Copied" : label}
    </button>
  );
}

export function CodeBlock({ code }: { code: string }) {
  return (
    <div className="code-block">
      <pre>{code}</pre>
      <CopyButton text={code} />
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle && <p className="muted">{subtitle}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(withZone(iso)).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "never";
  const seconds = Math.round((Date.now() - new Date(withZone(iso)).getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

// The API returns UTC; timestamps without an offset are treated as UTC, not local time.
function withZone(iso: string): string {
  return /[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`;
}
