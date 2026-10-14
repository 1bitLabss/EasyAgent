import type { ChatMessage } from "@/types";

export function chipText(message: Pick<ChatMessage, "chip" | "seen_step" | "refuse_reason">): string {
  if (message.chip === "queued") return "Queued";
  if (message.chip === "seen") {
    const step = message.seen_step || 0;
    return step > 0 ? `Seen by bot (folded in at step ${step})` : "Seen by bot";
  }
  if (message.chip === "answered") return "Answered";
  if (message.chip === "refused") {
    return message.refuse_reason || "The queue is full, so that message was not added. The job was not restarted.";
  }
  return "";
}
