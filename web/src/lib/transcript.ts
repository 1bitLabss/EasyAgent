import type { ChatMessage } from "@/types";

export function messageHasBubble(message: Pick<ChatMessage, "content" | "attachment" | "choices" | "error">): boolean {
  if (message.error) return true;
  if ((message.content || "").trim()) return true;
  if (message.attachment) return true;
  if ((message.choices || []).length) return true;
  return false;
}

export function reactionWho(reactionBy: string | undefined, botName: string): string {
  if (reactionBy === "bot") return botName || "the bot";
  return "you";
}

export function hideWhileSending(message: Pick<ChatMessage, "live">, sending: boolean): boolean {
  return Boolean(sending && message.live);
}

export function rowKey(message: Pick<ChatMessage, "client_id" | "id">, index: number): string {
  return message.client_id || message.id || `idx-${index}`;
}

/** Screen order is the saved order. A message does not move when a reply starts or ends. */
export function arrangeBusy(rows: ChatMessage[], _pinned: Set<string>): { head: ChatMessage[]; tail: ChatMessage[] } {
  return { head: rows, tail: [] };
}

function skippableRow(row: ChatMessage): boolean {
  return row.kind === "status" || row.kind === "pickup" || row.kind === "activity" || row.about === "activity" || row.about === "queue";
}

function queuedDuringReply(row: ChatMessage): boolean {
  if (row.role !== "user" || row.before_id) return false;
  return row.chip === "queued" || row.chip === "seen" || row.chip === "held";
}

/** The open reply stays where it started. Later user lines stay below it. A new reply goes last. */
export function placeLiveReply(rows: ChatMessage[], live: ChatMessage, stamp: string, replyId: string): ChatMessage[] {
  const index = rows.findIndex((row) => {
    if (row.role !== "assistant" || skippableRow(row)) return false;
    if (stamp && row.client_id === stamp) return true;
    if (replyId && row.id === replyId) return true;
    return false;
  });
  if (index >= 0) {
    const next = rows.slice();
    const answer = rows[index];
    next[index] = {
      ...answer,
      content: live.content || answer.content,
      thinking: live.thinking || answer.thinking,
      thought_seconds: live.thought_seconds ?? answer.thought_seconds,
      client_id: answer.client_id || stamp,
      live: undefined,
    };
    return next;
  }
  const painted: ChatMessage = { ...live, role: "assistant", client_id: live.client_id || stamp, live: undefined };
  let at = rows.length;
  while (at > 0) {
    const row = rows[at - 1];
    if (!row) break;
    if (skippableRow(row) || queuedDuringReply(row)) {
      at -= 1;
      continue;
    }
    break;
  }
  return [...rows.slice(0, at), painted, ...rows.slice(at)];
}

export function answerIndex(rows: ChatMessage[]): number {
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const row = rows[index];
    if (!row || row.role === "user") break;
    if (row.role !== "assistant") continue;
    if (row.kind === "status" || row.kind === "pickup" || row.kind === "activity" || row.about === "activity") continue;
    if ((row.content || "").trim() || (row.thinking || "").trim() || row.error) return index;
  }
  return -1;
}
