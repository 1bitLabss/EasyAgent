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
