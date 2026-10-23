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
    Boolean(left.interrupted) === Boolean(right.interrupted) &&
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

export function sameRun(left: Chat["run"], right: Chat["run"]) {
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

const PAUSE_SENTENCES = new Set([
  "Paused, you sent a message",
  "Paused, say continue to resume",
  "Finished before pausing",
]);

/** A pause row above a person's message is not dropped when a later snapshot omits it.

 A resolved cut is not put back, and it is not relabeled finished. Stop clears
 the row. A you-sent row that settles with the reply already done stays one
 line so the message under it does not jump.
 */
function holdPauseAboveUser(
  prev: ChatMessage[] | undefined,
  incoming: ChatMessage[],
  settled: boolean,
  cleared = false,
): ChatMessage[] {
  if (!prev) return incoming;
  const pauseAt = prev.findIndex(
    (message) => message.kind === "status" && (message.client_id === "pause-you-sent" || PAUSE_SENTENCES.has(message.content || "")),
  );
  if (pauseAt < 0) return incoming;
  const prior = prev[pauseAt];
  const user = prev.slice(pauseAt + 1).find((message) => message.role === "user");
  if (!prior || !user) return incoming;
  const sameUser = (message: ChatMessage) =>
    message.role === "user" &&
    ((user.client_id && message.client_id === user.client_id) ||
      (user.id && message.id === user.id) ||
      message.content === user.content);
  const userAt = incoming.findIndex(sameUser);
  if (userAt < 0) return incoming;
  if (prior.id && incoming.some((message) => message.id === prior.id)) return incoming;
  if (cleared || (settled && prior.content === "Paused, say continue to resume")) return incoming;
  const before = incoming[userAt - 1];
  if (before?.kind === "status" && PAUSE_SENTENCES.has(before.content || "")) {
    if (before.client_id || !prior.client_id) return incoming;
    const next = incoming.slice();
    next[userAt - 1] = { ...before, client_id: prior.client_id };
    return next;
  }
  const next = incoming.slice();
  next.splice(userAt, 0, {
    ...prior,
    role: "assistant",
    kind: "status",
    content: settled ? "Finished before pausing" : prior.content || "Paused, you sent a message",
    client_id: prior.client_id || "pause-you-sent",
  });
  return next;
}

/** The optimistic pause row and the saved one are the same sentence. Keep the client key so the bubble under it does not remount. */
function adoptPauseClient(prev: ChatMessage[] | undefined, incoming: ChatMessage[]): ChatMessage[] {
  const local = prev?.find((message) => !message.id && message.client_id === "pause-you-sent" && message.kind === "status");
  if (!local?.content) return incoming;
  let used = false;
  return incoming.map((message) => {
    if (used || message.client_id || message.kind !== "status" || message.content !== local.content) return message;
    used = true;
    return { ...message, client_id: local.client_id };
  });
}

function replySettled(status: string | undefined): boolean {
  return status === "idle" || status === "stopped" || status === "error";
}

/** Keep a just-sent row when a poll or refresh snapshot has not caught up. */
export function retainLocalTail(
  prev: ChatMessage[] | undefined,
  incoming: ChatMessage[],
  settled = false,
  cleared = false,
): ChatMessage[] {
  const rows = adoptPauseClient(prev, holdPauseAboveUser(prev, incoming, settled, cleared));
  if (!prev?.length) return dedupeMessages(rows);
  const incomingIds = new Set(rows.filter((message) => message.id).map((message) => message.id as string));
  const incomingClients = new Set(rows.filter((message) => message.client_id).map((message) => message.client_id as string));
  const keepLocal = (message: ChatMessage | undefined) => {
    if (!message || message.role !== "user" || message.id) return false;
    if (message.client_id && incomingClients.has(message.client_id)) return false;
    const echoed = rows.some(
      (item) => item.role === "user" && item.content === message.content && Boolean(item.id || item.client_id),
    );
    return !echoed;
  };
  let split = prev.length;
  let shared = false;
  for (let index = prev.length - 1; index >= 0; index -= 1) {
    const id = prev[index]?.id;
    if (id && incomingIds.has(id)) {
      split = index + 1;
      shared = true;
      break;
    }
  }
  if (!shared) {
    const local = prev.filter((message) => keepLocal(message));
    if (!local.length) return dedupeMessages(rows);
    return dedupeMessages([...rows, ...local]);
  }
  const tail = prev.slice(split).filter((message) => {
    if (!message) return false;
    if (message.client_id && incomingClients.has(message.client_id)) return false;
    if (message.id && incomingIds.has(message.id)) return false;
    if (!message.id && message.role === "user") {
      const echoed = rows.some(
        (item) => item.role === "user" && item.content === message.content && Boolean(item.id || item.client_id),
      );
      return !echoed;
    }
    if (message.about === "activity" || message.about === "queue" || message.id === "activity" || message.id === "live-status") return false;
    if (message.kind === "status" && message.about === "queue") return false;
    if (message.id) return true;
    return false;
  });
  if (!tail.length) return dedupeMessages(rows);
  return dedupeMessages([...rows, ...tail]);
}

/** Keep the previous chat object when a poll repeats the transcript. */
export function shareChat(prev: unknown, incoming: unknown): unknown {
  if (!prev || !incoming || typeof prev !== "object" || typeof incoming !== "object") return incoming;
  const older = prev as Chat;
  const next = incoming as Chat;
  if (!Array.isArray(next.messages)) return incoming;
  const messages = retainLocalTail(
    older.messages,
    shareMessages(older.messages, next.messages),
    replySettled(next.run?.status),
    next.run?.status === "stopped" || next.run?.status === "error",
  );
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
    const kept = retainLocalTail(
      prev.messages,
      incomingMessages,
      replySettled(incoming.run?.status),
      incoming.run?.status === "stopped" || incoming.run?.status === "error",
    );
    return windowChat({ ...incoming, messages: kept, message_count: Math.max(total, kept.length) });
  }
  const lastId = base[base.length - 1]?.id;
  const at = lastId ? incomingMessages.findIndex((message) => message.id === lastId) : -1;
  if (at < 0) {
    return windowChat({ ...incoming, messages: incomingMessages, message_count: total });
  }
  const byId = new Map(incomingMessages.map((message) => [message.id, message]));
  const updated = base.map((message) => (message.id && byId.get(message.id)) || message);
  const merged = retainLocalTail(
    base,
    [...updated, ...incomingMessages.slice(at + 1)],
    replySettled(incoming.run?.status),
    incoming.run?.status === "stopped" || incoming.run?.status === "error",
  );
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
