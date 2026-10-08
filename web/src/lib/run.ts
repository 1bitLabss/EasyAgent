export type RunStatus = "idle" | "running" | "stopped" | "error" | string;

export type FaceName =
  | "idle"
  | "waiting"
  | "thinking"
  | "tool"
  | "talking"
  | "halted"
  | "reconnecting";

export function runTone(step: string, status: string): Exclude<FaceName, "idle" | "talking"> {
  if (status === "stopped" || status === "error") return "halted";
  const text = String(step || "");
  if (text.startsWith("Model not answering")) return "reconnecting";
  if (text.startsWith("Queued:") || text.startsWith("Waiting") || text.startsWith("still waiting")) return "waiting";
  if (
    text.startsWith("Running") ||
    text === "Looking" ||
    text === "Remembering" ||
    text === "Connecting" ||
    text === "Searching" ||
    text.startsWith("Searching")
  ) {
    return "tool";
  }
  return "thinking";
}

export function faceStateFor(input: {
  sending?: boolean;
  phase?: string;
  label?: string;
  text?: string;
  runStatus?: string;
  step?: string;
  busy?: boolean;
}): FaceName {
  const sending = Boolean(input.sending);
  const status = input.runStatus || "";
  const live = sending || status === "running";
  if (input.phase === "stopped" || input.phase === "error" || status === "stopped" || status === "error") {
    return "halted";
  }
  if (!live) return input.busy ? "thinking" : "idle";
  const tone = runTone(input.label || input.step || "", status || "running");
  if (tone === "reconnecting") return "reconnecting";
  if ((input.text || "").length) return "talking";
  return tone;
}

export function formatElapsed(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(whole / 60);
  const rest = whole % 60;
  if (minutes <= 0) return `${rest}s`;
  return `${minutes}m ${rest}s`;
}

export function describeRun(
  step: string,
  status: string,
  startedAt: string | null | undefined,
  lastActivityAt: string | null | undefined,
  heardAt: number | undefined,
  now: number,
): { words: string; elapsed: string } {
  const started = Date.parse(startedAt || "");
  const elapsed = Number.isNaN(started) ? "" : formatElapsed((now - started) / 1000);
  const server = Date.parse(lastActivityAt || "");
  const last = Math.max(heardAt || 0, Number.isNaN(server) ? 0 : server);
  let words = step || "Thinking";
  if (status === "running" && last && !words.startsWith("Queued:") && !words.startsWith("Model not answering")) {
    const quiet = Math.floor((now - last) / 1000);
    if (quiet >= 60) words = `still waiting on the model (${quiet}s)`;
  }
  return { words, elapsed };
}

function formatCount(value: number): string {
  return (Number(value) || 0).toLocaleString("en-US");
}

export function contextNote(ctx: Record<string, number> | undefined, where: string): string {
  const source = ctx || {};
  const total = source.transcript_messages || 0;
  const chars = source.transcript_chars || 0;
  const shown = source.model_messages || 0;
  const folded = source.compacted_messages || 0;
  const used = source.context_tokens || 0;
  const limit = source.max_context_tokens || 0;
  const saved = `${total} message${total === 1 ? "" : "s"} saved (${formatCount(chars)} characters, about ${formatCount(source.transcript_tokens || 0)} tokens).`;
  if (!total) return limit ? `No messages yet. Each reply can use up to ${formatCount(limit)} tokens of this ${where}.` : "";
  const budget = limit ? ` (about ${formatCount(used)} of ${formatCount(limit)} tokens)` : "";
  if (!folded) return `${saved} The next reply sees all of them in full${budget}. Nothing compacted.`;
  return `${saved} The next reply sees the newest ${shown} in full and a summary of the ${folded} older ones${budget}.`;
}
