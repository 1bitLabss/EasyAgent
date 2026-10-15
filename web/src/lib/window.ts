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
    left.unverified === right.unverified &&
    JSON.stringify(left.receipts || []) === JSON.stringify(right.receipts || []) &&
    left.speaker_name === right.speaker_name &&
    left.kind === right.kind &&
    left.chip === right.chip &&
    left.seen_step === right.seen_step &&
    left.refuse_reason === right.refuse_reason &&
    (left.client_id || "") === (right.client_id || "") &&
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
  return left.id === right.id && left.status === right.status && left.started_at === right.started_at && left.last_activity_at === right.last_activity_at && left.current_step === right.current_step && left.reason === right.reason && left.activity_started_at === right.activity_started_at;
}

function looseKey(message: ChatMessage): string {
  if (message.client_id) return `c:${message.client_id}`;
  if (message.id) return `i:${message.id}`;
  if (message.role === "user") return `u:${message.content}`;
  return "";
}

/** One row per client id, server id, or unsaved user text. A failed send must not multiply. */
export function dedupeMessages(messages: ChatMessage[]): ChatMessage[] {
  const seen = new Set<string>();
  const out: ChatMessage[] = [];
  let dropped = false;
  for (const message of messages) {
    const key = looseKey(message);
    if (key) {
      if (seen.has(key)) {
        dropped = true;
        continue;
      }
      seen.add(key);
    }
    out.push(message);
  }
  return dropped ? out : messages;
}

/** Keep a just-sent row when a poll or refresh snapshot has not caught up. */
export function retainLocalTail(prev: ChatMessage[] | undefined, incoming: ChatMessage[]): ChatMessage[] {
  if (!prev?.length) return dedupeMessages(incoming);
  const incomingIds = new Set(incoming.filter((message) => message.id).map((message) => message.id as string));
  const incomingClients = new Set(incoming.filter((message) => message.client_id).map((message) => message.client_id as string));
  let split = prev.length;
  for (let index = prev.length - 1; index >= 0; index -= 1) {
    const id = prev[index]?.id;
    if (id && incomingIds.has(id)) {
      split = index + 1;
      break;
    }
    if (index === 0) split = 0;
  }
  const tail = prev.slice(split).filter((message) => {
    if (!message) return false;
    if (message.client_id && incomingClients.has(message.client_id)) return false;
    if (message.id && incomingIds.has(message.id)) return false;
    if (!message.id && message.role === "user") {
      const echoed = incoming.some(
        (item) => item.role === "user" && item.content === message.content && Boolean(item.id || item.client_id),
      );
      return !echoed;
    }
    if (message.id) return true;
    return false;
  });
  if (!tail.length) return dedupeMessages(incoming);
  return dedupeMessages([...incoming, ...tail]);
}

/** Keep the previous chat object when a poll repeats the transcript. */
export function shareChat(prev: unknown, incoming: unknown): unknown {
  if (!prev || !incoming || typeof prev !== "object" || typeof incoming !== "object") return incoming;
  const older = prev as Chat;
  const next = incoming as Chat;
  if (!Array.isArray(next.messages)) return incoming;
  const messages = retainLocalTail(older.messages, shareMessages(older.messages, next.messages));
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

/** A windowed poll (?window=80) must not drop history the reader already loaded above it. */
export function keepLoadedHistory(prev: unknown, incoming: unknown): unknown {
  if (!prev || !incoming || typeof prev !== "object" || typeof incoming !== "object") return incoming;
  const older = prev as Chat;
  const next = incoming as Chat;
  if (!older.messages?.length || !Array.isArray(next.messages) || older.id !== next.id) return incoming;
  if ((older.window_start ?? 0) >= (next.window_start ?? 0)) return incoming;
  return mergeChat(older, next);
}

export function mergeChat(prev: Chat | undefined, incoming: Chat): Chat {
  const incomingMessages = incoming.messages || [];
  const total = incoming.message_count ?? incomingMessages.length;
  if (!prev?.messages?.length) {
    return windowChat({ ...incoming, messages: incomingMessages, message_count: total });
  }
  const base = [...prev.messages];
  while (base.length && !base[base.length - 1]?.id && base[base.length - 1]?.role !== "user") base.pop();
  const prevStart = prev.window_start ?? 0;
  const loadedHistory = prevStart > 0 || base.length > TAIL;
  if (!loadedHistory) {
    const kept = retainLocalTail(prev.messages, incomingMessages);
    return windowChat({ ...incoming, messages: kept, message_count: Math.max(total, kept.length) });
  }
  const lastId = base[base.length - 1]?.id;
  const at = lastId ? incomingMessages.findIndex((message) => message.id === lastId) : -1;
  if (at < 0) {
    return windowChat({ ...incoming, messages: incomingMessages, message_count: total });
  }
  const byId = new Map(incomingMessages.map((message) => [message.id, message]));
  const updated = base.map((message) => (message.id && byId.get(message.id)) || message);
  const merged = retainLocalTail(base, [...updated, ...incomingMessages.slice(at + 1)]);
  return {
    ...incoming,
    messages: merged,
    message_count: Math.max(total, merged.length),
    window_start: prevStart,
  };
}

export type MessagePage = {
  messages: ChatMessage[];
  start: number;
  end: number;
  total: number;
};
