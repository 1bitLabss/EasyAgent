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
