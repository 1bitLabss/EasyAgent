import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { formatElapsed } from "@/lib/run";

export function stickToBottom(scrollHeight: number, scrollTop: number, clientHeight: number) {
  return scrollHeight - scrollTop - clientHeight < 24;
}

export function thoughtLabel(seconds: number | undefined) {
  if (seconds == null || Number.isNaN(seconds)) return "";
  return `Thought for ${formatElapsed(Math.max(0, Math.floor(seconds)))}`;
}

export function ThinkingBox({
  text,
  streaming = false,
  answering = false,
  seconds,
  startedAt,
  now,
}: {
  text?: string;
  streaming?: boolean;
  /** Hermes closes a finished reasoning group once the answer text starts. */
  answering?: boolean;
  seconds?: number;
  startedAt?: number;
  now?: number;
}) {
  const body = (text || "").trim();
  const [opened, setOpened] = useState<boolean | null>(null);
  const stick = useRef(true);
  const scroller = useRef<HTMLDivElement>(null);
  const [tick, setTick] = useState(() => Date.now());
  // A reasoning-only turn stays open after it settles. The answer closes it.
  const keepOpen = useRef(false);
  if (streaming && !answering) keepOpen.current = true;
  if (answering) keepOpen.current = false;
  useEffect(() => {
    if (!streaming) return;
    const timer = window.setInterval(() => setTick(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [streaming]);
  const expanded = opened !== null ? opened : (streaming ? !answering : keepOpen.current);
  const elapsed = seconds ?? (startedAt ? Math.max(0, Math.round(((now || tick) - startedAt) / 1000)) : undefined);
  const clock = thoughtLabel(elapsed);
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el || !expanded || !streaming || !stick.current) return;
    el.scrollTop = el.scrollHeight;
  }, [body, expanded, streaming]);
  if (!body) return null;
  return (
    <section className="thinking-box" data-testid="thinking-panel">
      <button
        type="button"
        className="thinking-head"
        aria-expanded={expanded}
        onClick={() => setOpened(!expanded)}
      >
        <span aria-hidden="true">{expanded ? "▾" : "▸"}</span>
        Thinking{clock ? ` · ${clock}` : ""}
      </button>
      {expanded ? (
        <div
          ref={scroller}
          className="thinking-body"
          data-testid="thinking-body"
          onScroll={() => {
            const el = scroller.current;
            if (!el) return;
            stick.current = stickToBottom(el.scrollHeight, el.scrollTop, el.clientHeight);
          }}
        >
          {body}
        </div>
      ) : null}
    </section>
  );
}
