import type { ChatMessage } from "@/types";

export function chipText(message: Pick<ChatMessage, "chip" | "seen_step" | "refuse_reason">): string {
  if (message.chip === "queued") return "Queued";
  if (message.chip === "seen") {
    const step = message.seen_step || 0;
    return step > 0 ? `Seen · step ${step}` : "Seen by bot";
  }
  if (message.chip === "answered") return "Answered";
  if (message.chip === "held") {
    return message.refuse_reason || "The reply failed, so this message is held. Retry to send it again.";
  }
  if (message.chip === "unsent") return message.refuse_reason || "Not sent (job stopped)";
  if (message.chip === "refused") {
    return message.refuse_reason || "The queue is full, so that message was not added. The job was not restarted.";
  }
  return "";
}

export function chipTitle(message: Pick<ChatMessage, "chip" | "seen_step" | "refuse_reason">): string {
  if (message.chip === "seen" && (message.seen_step || 0) > 0) {
    return `Seen by bot (folded in at step ${message.seen_step})`;
  }
  return chipText(message);
}
