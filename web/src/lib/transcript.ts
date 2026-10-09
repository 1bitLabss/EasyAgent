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
