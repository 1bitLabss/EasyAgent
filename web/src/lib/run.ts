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
    text.startsWith("Writing ") ||
    text.startsWith("Reading ") ||
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

export function workingLine(
  run: { step_index?: number; step_count?: number; started_at?: string | null; current_step?: string; last_activity?: string; queued?: number; stuck?: boolean } | null | undefined,
  now: number,
): { text: string; queued: boolean; stuck: boolean } {
  const count = run?.step_count || 0;
  const index = run?.step_index || 0;
  const started = Date.parse(run?.started_at || "");
  const elapsed = Number.isNaN(started) ? "" : formatElapsed((now - started) / 1000);
  const step = run?.current_step || "";
  const reconnecting = step.startsWith("Model not answering");
  const activity = reconnecting ? step : run?.last_activity || step;
  const bits: string[] = [];
  if (!reconnecting && count) bits.push(index ? `Step ${index} of ${count}` : `${count} steps`);
  if (elapsed) bits.push(elapsed);
  if (activity) bits.push(activity);
  return { text: bits.join(" · "), queued: (run?.queued || 0) > 0, stuck: reconnecting ? false : Boolean(run?.stuck) };
}

function activityBase(step: string): string {
  return step
    .replace(/, \d+m \d+s(?:, .* tokens so far)?$/, "")
    .replace(/, \d+s(?:, .* tokens so far)?$/, "");
}

function genericWait(step: string): boolean {
  return !step || step === "Thinking" || step === "Waiting" || step.startsWith("Waiting on model");
}

/** A tool that is still running. "Writing, 4s" is the model. "Writing harbor.py" is a tool. */
export function isToolStatus(step?: string): boolean {
  const text = activityBase((step || "").trim());
  if (!text || text.startsWith("Paused")) return false;
  const folded = text.toLowerCase();
  if (folded === "thinking" || folded === "waiting" || folded === "writing" || folded === "revising" || folded === "asking") return false;
  if (folded.startsWith("waiting on model") || folded.startsWith("model not answering") || folded.startsWith("still waiting")) return false;
  return true;
}

/** A placeholder wait must not cover a step the server already named. */
export function shownStep(label?: string, serverStep?: string): string {
  const live = (label || "").trim();
  const server = (serverStep || "").trim();
  if (live.startsWith("Paused")) return live;
  if (server.startsWith("Paused")) return server;
  if (isToolStatus(server) && !isToolStatus(live)) return server;
  if (genericWait(live) && !genericWait(server)) return server;
  return live || server;
}

export type RunPresence = "idle" | "working" | "waiting";

/** Idle, a tool step, or the model. Active states include the elapsed time. */
export function runPresence(input: {
  active: boolean;
  step?: string;
  startedAt?: string | null;
  now: number;
}): { kind: RunPresence; text: string } {
  if (!input.active) return { kind: "idle", text: "Idle" };
  const step = activityBase((input.step || "").trim());
  const started = Date.parse(input.startedAt || "");
  const elapsed = formatElapsed((input.now - (Number.isNaN(started) ? input.now : started)) / 1000);
  if (step.startsWith("Paused")) return { kind: "waiting", text: step };
  if (step === "Writing" || step.startsWith("Writing,")) return { kind: "working", text: step.startsWith("Writing,") ? step : `Writing, ${elapsed}` };
  const waiting = !step
    || step === "Thinking"
    || step === "Waiting"
    || step.startsWith("Waiting on model")
    || step.startsWith("Model not answering")
    || step.startsWith("still waiting")
    || step === "Revising";
  if (waiting) return { kind: "waiting", text: `Waiting on model, ${elapsed}` };
  return { kind: "working", text: `Working, ${elapsed}${step ? ` · ${step}` : ""}` };
}

/** The line under the transcript while a job is running. It ticks with `now`. */
export function activityLine(input: {
  step?: string;
  jobStartedAt?: string | null;
  activityStartedAt?: string | null;
  now: number;
  tokens?: number;
}): string {
  const step = (input.step || "").trim();
  const key = activityBase(step);
  const model = !key || key === "Thinking" || key.startsWith("Waiting on model") || key === "Waiting";
  const tool = /^(Running|Writing|Reading)\b/.test(key);
  if (!model && !tool) return step || "Working";
  const started = Date.parse(input.activityStartedAt || input.jobStartedAt || "");
  const elapsed = formatElapsed((input.now - (Number.isNaN(started) ? input.now : started)) / 1000);
  if (model) {
    const tokens = input.tokens || 0;
    if (tokens > 0 || key === "Writing" || step.startsWith("Writing")) return `Writing, ${elapsed}`;
    if (step.startsWith("Waiting on model,")) return step;
    return `Waiting on model, ${elapsed}`;
  }
  return `${key}, ${elapsed}`;
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
  if (
    status === "running" &&
    last &&
    !words.startsWith("Queued:") &&
    !words.startsWith("Model not answering") &&
    !words.startsWith("Waiting on model") &&
    !words.startsWith("Writing ") &&
    !words.startsWith("Reading ") &&
    !words.startsWith("Running")
  ) {
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
