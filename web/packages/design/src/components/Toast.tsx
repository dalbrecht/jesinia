// SPDX-License-Identifier: AGPL-3.0-or-later
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { Button } from "./Button";
import { VisuallyHidden } from "./Spinner";
import s from "./Toast.module.css";
import { cx } from "./util";

export type ToastTone = "info" | "success" | "warn" | "danger";
export type ToastOptions = {
  tone?: ToastTone;
  message: string;
  action?: { label: string; onAction: () => void };
  duration?: number | "persistent";
};
type Entry = ToastOptions & { id: string };

const ICONS: Record<ToastTone, string> = { info: "i", success: "✓", warn: "!", danger: "×" };
const DEFAULT_MS = 5000;
const MAX_VISIBLE = 3;

export function Toast({
  id,
  tone = "info",
  message,
  action,
  onDismiss,
}: ToastOptions & {
  id: string;
  onDismiss: (id: string) => void;
}) {
  return (
    <div className={cx(s.toast, s[tone])} data-toast={id} data-tone={tone}>
      <span className={s.icon} aria-hidden="true">
        {ICONS[tone]}
      </span>
      <span className={s.message}>{message}</span>
      {action ? (
        <Button variant="ghost" size="sm" onClick={action.onAction}>
          {action.label}
        </Button>
      ) : null}
      <Button variant="ghost" size="sm" aria-label="Dismiss" icon="×" onClick={() => onDismiss(id)} />
    </div>
  );
}

function lifetime(t: Entry): number | null {
  if (t.duration === "persistent" || t.action) return null;
  if (typeof t.duration === "number") return t.duration;
  return t.tone === "warn" || t.tone === "danger" ? null : DEFAULT_MS;
}

type Api = { show: (o: ToastOptions) => string; dismiss: (id: string) => void };
const ToastContext = createContext<Api | null>(null);

export function useToast(): Api {
  const api = useContext(ToastContext);
  if (!api) throw new Error("useToast must be used inside <ToastProvider>");
  return api;
}

let counter = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [visible, setVisible] = useState<Entry[]>([]);
  const [queue, setQueue] = useState<Entry[]>([]);
  const [polite, setPolite] = useState("");
  const [assertive, setAssertive] = useState("");
  const [paused, setPaused] = useState(false);
  const region = useRef<HTMLDivElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);

  const visibleRef = useRef<Entry[]>([]);
  const update = useCallback((next: Entry[]) => {
    visibleRef.current = next;
    setVisible(next);
  }, []);

  const dismiss = useCallback(
    (id: string) => {
      update(visibleRef.current.filter((t) => t.id !== id));
      setQueue((q) => q.filter((t) => t.id !== id));
    },
    [update],
  );

  const show = useCallback(
    (o: ToastOptions) => {
      const entry: Entry = { ...o, id: `toast-${++counter}` };
      if (entry.tone === "danger") setAssertive(entry.message);
      else setPolite(entry.message);
      const v = visibleRef.current;
      if (v.length < MAX_VISIBLE) {
        update([...v, entry]);
      } else {
        const oldest = v.findIndex((t) => lifetime(t) !== null);
        if (oldest === -1) setQueue((q) => [...q, entry]);
        else update([...v.slice(0, oldest), ...v.slice(oldest + 1), entry]);
      }
      return entry.id;
    },
    [update],
  );

  // Promote queued toasts when room frees up.
  useEffect(() => {
    if (visible.length < MAX_VISIBLE && queue.length > 0) {
      const [next, ...rest] = queue;
      if (next) {
        setQueue(rest);
        update([...visibleRef.current, next]);
      }
    }
  }, [visible.length, queue, update]);

  // Auto-dismiss; timers pause while the region is hovered or holds focus.
  useEffect(() => {
    if (paused) return;
    const timers = visible.flatMap((t) => {
      const ms = lifetime(t);
      return ms === null ? [] : [setTimeout(() => dismiss(t.id), ms)];
    });
    return () => timers.forEach(clearTimeout);
  }, [visible, paused, dismiss]);

  // F6 / ⌥⌘T: move focus into the region and back.
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      const jump = e.key === "F6" || (e.altKey && e.metaKey && (e.key === "t" || e.key === "†"));
      if (!jump || !region.current) return;
      const inside = region.current.contains(document.activeElement);
      if (!inside && visible.length === 0) return;
      e.preventDefault();
      if (inside) {
        returnFocus.current?.focus();
      } else {
        returnFocus.current = document.activeElement as HTMLElement | null;
        region.current.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [visible.length]);

  const onRegionKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Escape") return;
    const host = (e.target as HTMLElement).closest<HTMLElement>("[data-toast]");
    const id = host?.dataset.toast ?? visible[visible.length - 1]?.id;
    if (id) {
      e.preventDefault();
      dismiss(id);
      returnFocus.current?.focus();
    }
  };

  const api = useMemo(() => ({ show, dismiss }), [show, dismiss]);
  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        ref={region}
        role="region"
        aria-label="Notifications"
        tabIndex={-1}
        className={s.region}
        onMouseEnter={() => setPaused(true)}
        onMouseLeave={() => setPaused(false)}
        onFocus={() => setPaused(true)}
        onBlur={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setPaused(false);
        }}
        onKeyDown={onRegionKey}
      >
        {visible.map((t) => (
          <Toast key={t.id} {...t} onDismiss={dismiss} />
        ))}
        <div role="status" aria-live="polite">
          <VisuallyHidden>{polite}</VisuallyHidden>
        </div>
        <div role="alert">
          <VisuallyHidden>{assertive}</VisuallyHidden>
        </div>
      </div>
    </ToastContext.Provider>
  );
}
