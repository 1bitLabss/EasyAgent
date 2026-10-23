import type { ChatMessage } from "@/types";
import { formatElapsed } from "@/lib/run";

export type ActivityState = "running" | "done" | "failed";

export type ActivityStep = {
  id: string;
  text: string;
  state: ActivityState;
  count: number;
};

const SKIP = /^(thinking|waiting(?: on model)?|model not answering)\b/i;

function asState(value: string | undefined): ActivityState {
  if (value === "done" || value === "failed" || value === "running") return value;
  return "running";
}

/** One row per tool step. The same id updates in place. A repeated label in a row keeps a count. */
export function noteActivity(
  steps: ActivityStep[] | undefined,
  event: { text?: string; stepId?: string; state?: string },
): ActivityStep[] {
  const text = (event.text || "").replace(/\s+/g, " ").trim();
  const current = steps || [];
  // Pause sentences are the transcript row or the live status, never a tool step.
  // Leaving the previous step running keeps it off the finished-step list.
  if (!text || SKIP.test(text) || text.startsWith("Paused")) return current;
  const state = asState(event.state);
  const id = (event.stepId || "").trim();
  if (id) {
    const known = current.findIndex((item) => item.id === id);
    if (known >= 0) {
      const row = current[known];
      if (row.text === text && row.state === state) return current;
      const next = current.slice();
      next[known] = { ...row, text, state };
      return next;
    }
  }
  const last = current[current.length - 1];
  if (last && last.text === text && last.state === "running") {
    if (last.state === state && !id) return current;
    const next = current.slice();
    next[next.length - 1] = { ...last, state };
    return next;
  }
  const closed = last && last.state === "running"
    ? [...current.slice(0, -1), { ...last, state: "done" as const }]
    : current.slice();
  const previous = closed[closed.length - 1];
  if (previous && previous.text === text) {
    const next = closed.slice();
    next[next.length - 1] = {
      ...previous,
      state,
      count: previous.count + 1,
      id: previous.id,
    };
    return next;
  }
  const row: ActivityStep = { id: id || `step-${closed.length + 1}`, text, state, count: 1 };
  const appended = [...closed, row];
  return appended.length > 12 ? appended.slice(-12) : appended;
}

export function settleActivity(steps: ActivityStep[] | undefined, state: ActivityState = "done"): ActivityStep[] | undefined {
  if (!steps?.some((item) => item.state === "running")) return steps;
  return steps.map((item) => (item.state === "running" ? { ...item, state } : item));
}

export function activityLabel(step: ActivityStep): string {
  const times = step.count > 1 ? ` ×${step.count}` : "";
  if (step.state === "failed") return `${step.text}${times} — failed`;
  return `${step.text}${times}`;
}

function failureReason(message: ChatMessage): string {
  if (message.kind === "resume-offer") return "the job stopped";
  const text = (message.content || "").replace(/\s+/g, " ").trim();
  if (message.error || text === "Stopped." || text.startsWith("Stopped:")) {
    if (text.startsWith("Stopped:")) return text.slice("Stopped:".length).trim() || "the reply stopped";
    if (text === "Stopped.") return "the reply stopped";
    return text || "the reply failed";
  }
  return "";
}

function took(start?: string, end?: string): string {
  const opened = Date.parse(start || "");
  const closed = Date.parse(end || "");
  if (Number.isNaN(opened) || Number.isNaN(closed) || closed < opened) return "";
  return formatElapsed((closed - opened) / 1000);
}

const SKIP_KIND = new Set(["status", "pickup", "activity", "yolo"]);

/** The line under the log: who was asked, and whether a reply came back. */
export function askedLine(endpoint: string, messages: ChatMessage[] | undefined): string {
  const who = (endpoint || "").trim() || "the model";
  const rows = messages || [];
  let outcome: ChatMessage | null = null;
  let ask: ChatMessage | null = null;
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const message = rows[index];
    if (!message || message.about === "activity") continue;
    if (SKIP_KIND.has(message.kind || "")) continue;
    if (message.role === "assistant") {
      if (!outcome) outcome = message;
      continue;
    }
    if (message.role === "user") {
      ask = message;
      break;
    }
  }
  if (!outcome) return `Asked ${who}`;
  const reason = failureReason(outcome);
  if (reason) return `Asked ${who} → failed: ${reason}`;
  const clock = took(ask?.created_at, outcome.created_at);
  return clock ? `Asked ${who} → got reply (${clock})` : `Asked ${who} → got reply`;
}
