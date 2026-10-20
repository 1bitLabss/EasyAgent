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

/** Busy lines stay under the in-progress reply instead of jumping above it. */
export function arrangeBusy(rows: ChatMessage[], pinned: Set<string>): { head: ChatMessage[]; tail: ChatMessage[] } {
  const head: ChatMessage[] = [];
  const tail: ChatMessage[] = [];
  const answerAt = answerIndex(rows);
  rows.forEach((row, index) => {
    const key = row.client_id || row.id || "";
    if (key && row.role === "user" && (row.chip === "queued" || row.chip === "seen")) pinned.add(key);
    const busy = row.role === "user" && (row.chip === "queued" || row.chip === "seen" || Boolean(key && pinned.has(key)));
    if (busy && (answerAt < 0 || index < answerAt)) tail.push(row);
    else head.push(row);
  });
  return { head, tail };
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
