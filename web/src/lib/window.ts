import type { Chat, ChatMessage } from "@/types";

export const TAIL = 80;
export const ESTIMATE = 72;
export const OVERSCAN = 10;

export function visibleRange(
  scrollTop: number,
  viewport: number,
  count: number,
  estimate = ESTIMATE,
  overscan = OVERSCAN,
): { start: number; end: number } {
  if (count <= 0) return { start: 0, end: 0 };
  const start = Math.max(0, Math.floor(Math.max(0, scrollTop) / estimate) - overscan);
  const end = Math.min(count, Math.ceil((Math.max(0, scrollTop) + Math.max(0, viewport)) / estimate) + overscan);
  return { start, end: Math.max(end, start) };
}

export function windowChat(chat: Chat): Chat {
  const messages = chat.messages || [];
  const total = chat.message_count ?? messages.length;
  const start = chat.window_start ?? 0;
  if (messages.length <= TAIL) {
    return { ...chat, messages, message_count: total, window_start: start };
  }
  const tail = messages.slice(-TAIL);
  return { ...chat, messages: tail, message_count: total, window_start: Math.max(0, total - tail.length) };
}

function sameList(left?: string[], right?: string[]) {
  if (left === right) return true;
  if (!left?.length && !right?.length) return true;
  if (!left || !right || left.length !== right.length) return false;
  return left.every((item, index) => item === right[index]);
}

export function sameMessage(left: ChatMessage, right: ChatMessage): boolean {
  return (
    left.id === right.id &&
    left.role === right.role &&
    left.content === right.content &&
    left.thinking === right.thinking &&
    left.reaction === right.reaction &&
    left.reaction_by === right.reaction_by &&
    left.error === right.error &&
    left.thought_seconds === right.thought_seconds &&
    left.check === right.check &&
    left.lesson === right.lesson &&
    left.speaker_name === right.speaker_name &&
    sameList(left.choices, right.choices) &&
    sameList(left.skills_saved, right.skills_saved) &&
    (left.attachment?.id || "") === (right.attachment?.id || "") &&
    (left.attachment?.excerpt || "") === (right.attachment?.excerpt || "")
  );
}

export function shareMessages(prev: ChatMessage[] | undefined, incoming: ChatMessage[]): ChatMessage[] {
  if (!prev?.length) return incoming;
  const byId = new Map(prev.filter((message) => message.id).map((message) => [message.id as string, message]));
  let same = prev.length === incoming.length;
  const next = incoming.map((message, index) => {
    const old = message.id ? byId.get(message.id) : undefined;
    const kept = old && sameMessage(old, message) ? old : message;
    if (kept !== prev[index]) same = false;
    return kept;
  });
  return same ? prev : next;
}

function sameRun(left: Chat["run"], right: Chat["run"]) {
  if (left === right) return true;
  if (!left || !right) return !left && !right;
  return left.id === right.id && left.status === right.status && left.started_at === right.started_at && left.last_activity_at === right.last_activity_at && left.current_step === right.current_step && left.reason === right.reason;
}

/** Keep the previous chat object when a poll repeats the transcript. */
export function shareChat(prev: unknown, incoming: unknown): unknown {
  if (!prev || !incoming || typeof prev !== "object" || typeof incoming !== "object") return incoming;
  const older = prev as Chat;
  const next = incoming as Chat;
  if (!Array.isArray(next.messages)) return incoming;
  const messages = shareMessages(older.messages, next.messages);
  if (
    messages === older.messages &&
    older.id === next.id &&
    older.title === next.title &&
    older.message_count === next.message_count &&
    older.window_start === next.window_start &&
    older.updated_at === next.updated_at &&
    sameRun(older.run, next.run)
  ) {
    return older;
  }
  return { ...next, messages };
}

/** Approval cards while a reply is running. Idle and hidden windows do not ask. */
export const APPROVAL_POLL_MS = 5000;

export function approvalPollMs(active: boolean, hidden = false): number | false {
  if (hidden || !active) return false;
  return APPROVAL_POLL_MS;
}

/** One unread poll per window. A hidden tab does not ask. */
export const UNREAD_POLL_MS = 8000;

export function unreadRefetchInterval(): number | false {
  if (typeof document !== "undefined" && document.hidden) return false;
  return UNREAD_POLL_MS;
}

export function mergeChat(prev: Chat | undefined, incoming: Chat): Chat {
  const incomingMessages = incoming.messages || [];
  const total = incoming.message_count ?? incomingMessages.length;
  if (!prev?.messages?.length) {
    return windowChat({ ...incoming, messages: incomingMessages, message_count: total });
  }
  const base = [...prev.messages];
  while (base.length && !base[base.length - 1]?.id) base.pop();
  const prevStart = prev.window_start ?? 0;
  const loadedHistory = prevStart > 0 || base.length > TAIL;
  if (!loadedHistory) {
    return windowChat({ ...incoming, messages: incomingMessages, message_count: total });
  }
  const lastId = base[base.length - 1]?.id;
  const at = lastId ? incomingMessages.findIndex((message) => message.id === lastId) : -1;
  if (at < 0) {
    return windowChat({ ...incoming, messages: incomingMessages, message_count: total });
  }
  const byId = new Map(incomingMessages.map((message) => [message.id, message]));
  const updated = base.map((message) => (message.id && byId.get(message.id)) || message);
  return {
    ...incoming,
    messages: [...updated, ...incomingMessages.slice(at + 1)],
    message_count: total,
    window_start: prevStart,
  };
}

export type MessagePage = {
  messages: ChatMessage[];
  start: number;
  end: number;
  total: number;
};
