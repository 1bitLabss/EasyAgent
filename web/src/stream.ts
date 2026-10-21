import { useSyncExternalStore } from "react";
import { api, queryClient, streamPost } from "@/api";
import { noteActivity, settleActivity, type ActivityStep } from "@/lib/activity";
import { activityLine } from "@/lib/run";
import { mergeChat, sameRun } from "@/lib/window";
import { moodForBadge, pokeFace } from "@/lib/mood";
import { useApp } from "@/store";
import type { Chat, ChatMessage, Run } from "@/types";

// Flush cadence ported from Hermes desktop (MIT, Nous Research):
// apps/desktop/src/app/session/hooks/use-message-stream/utils.ts
// and scheduleDeltaFlush in use-message-stream/index.ts.
// A timer, not rAF: a hidden or occluded Tauri webview pauses animation frames,
// so a finished reply would sit unpainted until the window is focused again.
export const STREAM_DELTA_FLUSH_MS = 33;
export const MAX_STREAM_FLUSH_GAP_MS = 250;

export function streamFlushDelay(sinceLastMs: number, lastCostMs: number): number {
  const floor = Math.min(Math.max(STREAM_DELTA_FLUSH_MS, lastCostMs * 3), MAX_STREAM_FLUSH_GAP_MS);
  return Math.max(0, floor - sinceLastMs);
}

export type Live = {
  phase: "thinking" | "reply" | "stopped" | "error";
  label: string;
  text: string;
  reasoning: string;
  run: Run | null;
  heardAt: number;
  steps?: ActivityStep[];
  thoughtAt?: number;
  thoughtSeconds?: number;
  reasoningLive?: boolean;
  aside?: string;
  chars?: number;
  waitAt?: number;
  replyId?: string;
};

export type FrozenReply = {
  replyId: string;
  text: string;
  reasoning: string;
};

export type Flight = {
  botId: string;
  chatId: string;
  runId: string;
  control: AbortController;
  live: Live | null;
  sending: boolean;
  replaced: boolean;
  settled: boolean;
  /** The partial as it looked when the user row was inserted. It does not grow after that. */
  frozen?: FrozenReply;
};

export const PAUSE_TEXT = "Paused, reading your message";
export const PAUSE_TOOL_TEXT = "Paused, reading your message — finishing current step";

/** A named tool step, not the model clock. */
export function isToolLabel(label: string): boolean {
  const text = (label || "").trim();
  if (!text || text.startsWith("Paused")) return false;
  if (text.startsWith("Waiting") || text === "Thinking" || text === "Revising") return false;
  if (text.startsWith("Writing,") || text === "Writing") return false;
  if (text.startsWith("Model not answering")) return false;
  return text.startsWith("Running") || text.startsWith("Reading") || text.startsWith("Writing ") || text.startsWith("Searching");
}

const flights = new Map<string, Flight>();
const settledRuns = new Map<string, { runId: string; at: number }>();
const flightListeners = new Map<string, Set<() => void>>();
const botListeners = new Map<string, Set<() => void>>();
const botEpoch = new Map<string, number>();

type FlushClock = {
  timer: ReturnType<typeof setTimeout> | null;
  lastAt: number;
  cost: number;
  raf: number | null;
  startedAt: number;
};

const clocks = new Map<string, FlushClock>();

function clockFor(key: string): FlushClock {
  let clock = clocks.get(key);
  if (!clock) {
    clock = { timer: null, lastAt: 0, cost: 0, raf: null, startedAt: 0 };
    clocks.set(key, clock);
  }
  return clock;
}

function notifyFlight(flight: Flight) {
  const key = flightKey(flight.botId, flight.chatId);
  botEpoch.set(flight.botId, (botEpoch.get(flight.botId) || 0) + 1);
  flightListeners.get(key)?.forEach((cb) => cb());
  botListeners.get(flight.botId)?.forEach((cb) => cb());
}

export function subscribeFlight(key: string, cb: () => void) {
  let set = flightListeners.get(key);
  if (!set) {
    set = new Set();
    flightListeners.set(key, set);
  }
  set.add(cb);
  return () => {
    set?.delete(cb);
    if (set && set.size === 0) flightListeners.delete(key);
  };
}

function subscribeBot(botId: string, cb: () => void) {
  let set = botListeners.get(botId);
  if (!set) {
    set = new Set();
    botListeners.set(botId, set);
  }
  set.add(cb);
  return () => {
    set?.delete(cb);
    if (set && set.size === 0) botListeners.delete(botId);
  };
}

export function useLive(botId: string | null, chatId: string | null): Live | null {
  const key = botId && chatId ? flightKey(botId, chatId) : "";
  return useSyncExternalStore(
    (cb) => (key ? subscribeFlight(key, cb) : () => undefined),
    () => (botId && chatId ? flightFor(botId, chatId)?.live ?? null : null),
    () => null,
  );
}

export function useFlightPulse(botId: string | null): number {
  return useSyncExternalStore(
    (cb) => (botId ? subscribeBot(botId, cb) : () => undefined),
    () => (botId ? botEpoch.get(botId) || 0 : 0),
    () => 0,
  );
}

export function flightKey(botId: string, chatId: string) {
  return `${botId}:${chatId}`;
}

export function flightFor(botId: string, chatId: string): Flight | null {
  return flights.get(flightKey(botId, chatId)) || null;
}

export function resetStreamForTests() {
  for (const flight of flights.values()) flight.control.abort();
  for (const clock of clocks.values()) {
    if (clock.timer !== null) clearTimeout(clock.timer);
    if (clock.raf !== null && typeof cancelAnimationFrame === "function") cancelAnimationFrame(clock.raf);
  }
  flights.clear();
  settledRuns.clear();
  clocks.clear();
  botEpoch.clear();
}

export function setFlightForTests(botId: string, chatId: string, live: Live | null) {
  const key = flightKey(botId, chatId);
  if (!live) {
    flights.delete(key);
    return;
  }
  const flight: Flight = {
    botId,
    chatId,
    runId: live.run?.id || "",
    control: new AbortController(),
    live,
    sending: false,
    replaced: false,
    settled: false,
  };
  flights.set(key, flight);
  flushNow(flight);
}

function releaseRun(chat: Chat): Chat {
  if (chat.run?.status !== "running") return chat;
  return {
    ...chat,
    run: { id: "", status: "idle", started_at: null, last_activity_at: null, current_step: "", reason: "" },
  };
}

/** A run we already finished must not keep the transcript polling. A new run id is a new job. */
export function runIsFinished(botId: string, chatId: string, status?: string | null, runId?: string | null): boolean {
  const key = flightKey(botId, chatId);
  const done = settledRuns.get(key);
  if (!done) return false;
  if (runId && done.runId && runId !== done.runId) {
    settledRuns.delete(key);
    return false;
  }
  if (status !== "running") return true;
  if (!runId || !done.runId || runId === done.runId) return true;
  settledRuns.delete(key);
  return false;
}

export function chatStillPolling(botId: string, chatId: string, status?: string | null, runId?: string | null): number | false {
  if (runIsFinished(botId, chatId, status, runId)) return false;
  const flight = flightFor(botId, chatId);
  if (flight && !flight.replaced && flight.sending && flight.live && Date.now() - flight.live.heardAt < 5000) return false;
  return status === "running" ? 2000 : false;
}

export function settleIncoming(botId: string, chatId: string, chat: Chat): Chat {
  if (chat.run?.status !== "running") return chat;
  if (!runIsFinished(botId, chatId, chat.run.status, chat.run.id)) return chat;
  return releaseRun(chat);
}

export function anySending(botId: string): Flight | null {
  for (const flight of flights.values()) {
    if (flight.botId !== botId || flight.replaced) continue;
    if (flight.sending) return flight;
    if (flight.live?.run?.status === "running") return flight;
  }
  return null;
}

function visible(flight: Flight) {
  const state = useApp.getState();
  return state.screen === "chat" && state.botId === flight.botId && state.chatId === flight.chatId;
}

function paint(flight: Flight) {
  notifyFlight(flight);
  const run = flight.live?.run;
  if (!visible(flight) || !run) return;
  queryClient.setQueryData<Chat>(["chat", flight.botId, flight.chatId], (chat) => {
    if (!chat || !run) return chat;
    if (sameRun(chat.run, run)) return chat;
    return { ...chat, run };
  });
}

function flushNow(flight: Flight) {
  const key = flightKey(flight.botId, flight.chatId);
  const clock = clockFor(key);
  if (clock.timer !== null) {
    clearTimeout(clock.timer);
    clock.timer = null;
  }
  const startedAt = typeof performance !== "undefined" ? performance.now() : Date.now();
  clock.lastAt = startedAt;
  clock.startedAt = startedAt;
  paint(flight);
  const writeCost = (typeof performance !== "undefined" ? performance.now() : Date.now()) - startedAt;
  clock.cost = writeCost;
  if (typeof window === "undefined" || typeof window.requestAnimationFrame !== "function") return;
  if (clock.raf !== null) window.cancelAnimationFrame(clock.raf);
  clock.raf = window.requestAnimationFrame((frameStart) => {
    clock.raf = null;
    if (clock.startedAt !== startedAt) return;
    const now = typeof performance !== "undefined" ? performance.now() : Date.now();
    clock.cost = writeCost + Math.max(0, now - frameStart);
  });
}

function scheduleFlush(flight: Flight) {
  const key = flightKey(flight.botId, flight.chatId);
  const clock = clockFor(key);
  if (clock.timer !== null) return;
  if (typeof window === "undefined") {
    flushNow(flight);
    return;
  }
  const now = typeof performance !== "undefined" ? performance.now() : Date.now();
  clock.timer = window.setTimeout(() => {
    clock.timer = null;
    flushNow(flight);
  }, streamFlushDelay(now - clock.lastAt, clock.cost));
}

export function eventMatches(event: Record<string, unknown>, flight: Flight) {
  if (event.bot_id && event.bot_id !== flight.botId) return false;
  if (event.chat_id && event.chat_id !== flight.chatId) return false;
  const run = event.run as Run | undefined;
  const runId = String(event.run_id || run?.id || "");
  if (runId && runId !== flight.runId) {
    flight.runId = runId;
    if (run?.status === "running") flight.settled = false;
  }
  return true;
}

function remember(flight: Flight, live: Live, immediate = false) {
  flight.live = live;
  if (immediate) flushNow(flight);
  else scheduleFlush(flight);
}

function carry(live: Live, patch: Partial<Live>): Live {
  return {
    phase: patch.phase ?? live.phase,
    label: patch.label ?? live.label,
    text: patch.text ?? live.text,
    reasoning: patch.reasoning ?? live.reasoning,
    run: patch.run === undefined ? live.run : patch.run,
    heardAt: Date.now(),
    steps: patch.steps ?? live.steps,
    thoughtAt: patch.thoughtAt === undefined ? live.thoughtAt : patch.thoughtAt,
    thoughtSeconds: patch.thoughtSeconds === undefined ? live.thoughtSeconds : patch.thoughtSeconds,
    reasoningLive: patch.reasoningLive === undefined ? live.reasoningLive : patch.reasoningLive,
    aside: patch.aside === undefined ? live.aside : patch.aside,
    chars: patch.chars === undefined ? live.chars : patch.chars,
    waitAt: patch.waitAt === undefined ? live.waitAt : patch.waitAt,
    replyId: patch.replyId === undefined ? live.replyId : patch.replyId,
  };
}

function freezeThought(live: Live): number | undefined {
  if (live.thoughtSeconds) return live.thoughtSeconds;
  if (!live.thoughtAt) return undefined;
  return Math.max(1, Math.round((Date.now() - live.thoughtAt) / 1000));
}

export function noteSettled(botId: string, chatId: string, runId: string) {
  settledRuns.set(flightKey(botId, chatId), { runId, at: Date.now() });
}

function finish(flight: Flight, chat?: Chat) {
  const runId = flight.runId || flight.live?.run?.id || chat?.run?.id || "";
  noteSettled(flight.botId, flight.chatId, runId);
  flight.settled = true;
  flight.sending = false;
  flight.live = null;
  const key = ["chat", flight.botId, flight.chatId] as const;
  const prev = queryClient.getQueryData<Chat>(key);
  const next = chat ? releaseRun(chat) : prev ? releaseRun(prev) : undefined;
  if (next) {
    queryClient.setQueryData(key, mergeChat(prev, next));
    void queryClient.invalidateQueries({ queryKey: ["chats", flight.botId] });
    void queryClient.invalidateQueries({ queryKey: ["unread"] });
  }
  flushNow(flight);
  notifyFinished(flight, next);
}

function notifyFinished(flight: Flight, chat?: Chat) {
  if (inDesktop()) return;
  const last = chat?.messages?.slice(-1)[0];
  const asking = Boolean(last && last.role === "assistant" && (last.choices || []).length > 1);
  const body = asking ? "A bot has a question." : "A bot finished a reply.";
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  if (visible(flight) && document.visibilityState === "visible") return;
  try {
    new Notification("EasyAgent", { body });
  } catch {
    /* a browser can refuse the notice; the chat still has the reply */
  }
}

function inDesktop() {
  return typeof window !== "undefined" && Boolean((window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__);
}

function apply(event: Record<string, unknown>, flight: Flight) {
  if (!eventMatches(event, flight)) return;
  const live = flight.live || { phase: "thinking" as const, label: "Waiting on model", text: "", reasoning: "", run: null, heardAt: Date.now() };
  if (event.type === "run" && event.run) {
    const run = event.run as Run;
    const statusChanged = (live.run?.status || "") !== (run.status || "");
    const replyId = run.reply_id || live.replyId || "";
    const switched = Boolean(live.replyId && replyId && live.replyId !== replyId);
    if (switched) flight.frozen = undefined;
    const keepPause = Boolean(flight.frozen && (live.label || "").startsWith("Paused") && !(run.current_step || "").startsWith("Paused"));
    remember(flight, carry(live, {
      label: keepPause ? live.label : (run.current_step || live.label),
      run,
      replyId,
      text: flight.frozen ? flight.frozen.text : switched ? "" : live.text,
      reasoning: flight.frozen ? flight.frozen.reasoning : switched ? "" : live.reasoning,
      chars: switched ? 0 : live.chars,
    }), statusChanged || switched);
    return;
  }
  if (event.type === "sync" && event.chat && typeof event.chat === "object") {
    const chat = event.chat as Chat;
    const prev = queryClient.getQueryData<Chat>(["chat", flight.botId, flight.chatId]);
    queryClient.setQueryData(["chat", flight.botId, flight.chatId], mergeChat(prev, chat));
    const saved = [...(chat.messages || [])].reverse().find((item) => item.role === "assistant" && item.live);
    const replyId = chat.run?.reply_id || saved?.id || "";
    const keepPause = Boolean(flight.frozen && (live.label || "").startsWith("Paused") && !(chat.run?.current_step || "").startsWith("Paused"));
    remember(flight, carry(live, {
      phase: (saved?.content || "").trim() ? "reply" : "thinking",
      label: keepPause ? live.label : (chat.run?.current_step || live.label || "Writing"),
      text: flight.frozen ? flight.frozen.text : (saved?.content || ""),
      reasoning: flight.frozen ? flight.frozen.reasoning : (saved?.thinking || ""),
      run: chat.run || live.run,
      replyId: flight.frozen?.replyId || replyId,
      chars: flight.frozen ? flight.frozen.text.length : (saved?.content || "").length,
    }), true);
    return;
  }
  if (event.type === "pause") {
    if (event.chat && typeof event.chat === "object") {
      const chat = event.chat as Chat;
      const prev = queryClient.getQueryData<Chat>(["chat", flight.botId, flight.chatId]);
      queryClient.setQueryData(["chat", flight.botId, flight.chatId], mergeChat(prev, chat));
    }
    const run = (event.run as Run | undefined) || live.run;
    const label = String(event.text || "Paused, reading your message");
    if (flight.frozen) {
      remember(flight, carry(live, {
        phase: "reply",
        label,
        text: flight.frozen.text,
        reasoning: flight.frozen.reasoning,
        reasoningLive: false,
        run,
        replyId: flight.frozen.replyId || live.replyId,
        chars: flight.frozen.text.length,
        steps: settleActivity(live.steps),
      }), true);
      return;
    }
    remember(flight, carry(live, {
      phase: "thinking",
      label,
      text: "",
      reasoning: "",
      reasoningLive: false,
      run,
      replyId: "",
      chars: 0,
      steps: settleActivity(live.steps),
    }), true);
    return;
  }
  if (event.type === "stopped") {
    finish(flight, event.chat as Chat | undefined);
    return;
  }
  if (event.type === "user" && event.message && typeof event.message === "object") {
    placeMessage(flight, event.message as ChatMessage);
    flushNow(flight);
    return;
  }
  if (event.type === "note" && event.message && typeof event.message === "object") {
    placeMessage(flight, event.message as ChatMessage, { append: true });
    flushNow(flight);
    return;
  }
  if (event.type === "aside") {
    const run = (event.run as Run | undefined) || live.run;
    remember(flight, carry(live, { aside: String(event.text || ""), run }));
    return;
  }
  if (event.type === "status") {
    const run = (event.run as Run | undefined) || live.run;
    const label = String(event.text || "Thinking");
    if (run && event.text) {
      run.current_step = label;
      if (label.startsWith("Model not answering")) run.last_activity = label;
    }
    const steps = noteActivity(live.steps, {
      text: label,
      stepId: String(event.step_id || ""),
      state: String(event.state || ""),
    });
    const modelWait = label === "Thinking" || label.startsWith("Waiting on model");
    const keepPause = Boolean(flight.frozen && (live.label || "").startsWith("Paused") && !label.startsWith("Paused"));
    remember(flight, carry(live, {
      phase: "thinking",
      label: keepPause ? live.label : label,
      run,
      steps,
      chars: modelWait ? live.chars : 0,
      waitAt: modelWait ? live.waitAt || Date.now() : Date.now(),
    }));
    return;
  }
  if (event.type === "replay") {
    if (flight.frozen) return;
    const reasoning = String(event.text || "");
    remember(flight, carry(live, {
      phase: "thinking",
      label: live.label || "Model not answering, retrying…",
      reasoning,
      reasoningLive: Boolean(reasoning.trim()),
    }));
    return;
  }
  if (event.type === "replace") {
    remember(flight, carry(live, { phase: "thinking", label: "Searching", text: "" }));
    return;
  }
  if (event.type === "line") {
    remember(flight, carry(live, { phase: "thinking", label: live.label || "Thinking", text: live.text + String(event.text || "") }));
    return;
  }
  if (event.type === "thinking") {
    const chunk = String(event.text || "");
    const chars = (live.chars || 0) + chunk.length;
    const waitAt = live.waitAt || Date.now();
    const label = flight.frozen && (live.label || "").startsWith("Paused")
      ? live.label
      : activityLine({
        step: "Waiting on model",
        activityStartedAt: new Date(waitAt).toISOString(),
        now: Date.now(),
        tokens: Math.floor(chars / 4),
      });
    remember(flight, carry(live, {
      phase: live.phase === "reply" ? "reply" : "thinking",
      label,
      reasoning: flight.frozen ? flight.frozen.reasoning : live.reasoning + chunk,
      reasoningLive: true,
      thoughtAt: live.thoughtAt || Date.now(),
      chars,
      waitAt,
    }));
    return;
  }
  if (event.type === "delta") {
    const chunk = String(event.text || "");
    const chars = (live.chars || 0) + chunk.length;
    const waitAt = live.waitAt || Date.now();
    const label = flight.frozen && (live.label || "").startsWith("Paused")
      ? live.label
      : activityLine({
        step: "Writing",
        activityStartedAt: new Date(waitAt).toISOString(),
        now: Date.now(),
        tokens: Math.floor(chars / 4),
      });
    const reset = Boolean(event.reset);
    const nextText = reset ? chunk : chunk && live.text && chunk.startsWith(live.text) ? chunk : live.text + chunk;
    remember(flight, carry(live, {
      phase: "reply",
      label,
      text: flight.frozen ? flight.frozen.text : nextText,
      reasoning: flight.frozen ? flight.frozen.reasoning : live.reasoning,
      reasoningLive: false,
      thoughtSeconds: freezeThought(live),
      chars,
      waitAt,
      steps: settleActivity(live.steps),
    }));
    return;
  }
  if (event.type === "face") {
    const badge = moodForBadge(String(event.text || ""));
    const mood = event.text === "sad" || event.text === "glad" ? event.text : badge;
    if (mood === "glad" || mood === "sad") pokeFace(flight.botId, mood);
    return;
  }
  if (event.type === "error") {
    pokeFace(flight.botId, "sad");
    flight.sending = false;
    remember(flight, carry(live, {
      phase: "error",
      label: "Stopped",
      text: String(event.detail || "The reply failed."),
      reasoning: "",
      reasoningLive: false,
      steps: settleActivity(live.steps, "failed"),
    }), true);
    if (event.chat) finish(flight, event.chat as Chat);
    return;
  }
  if (event.type === "done" && event.chat) finish(flight, event.chat as Chat);
}

async function readEvents(response: Response, flight: Flight) {
  if (!response.body) throw new Error("The reply did not arrive.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let saw = false;
  let ended = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let split = buffer.indexOf("\n\n");
    while (split >= 0) {
      const raw = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      const data = raw
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim())
        .join("\n");
      if (data) {
        saw = true;
        const event = JSON.parse(data) as Record<string, unknown>;
        if (event.type === "done" || event.type === "error" || event.type === "stopped") ended = String(event.type);
        apply(event, flight);
      }
      split = buffer.indexOf("\n\n");
    }
  }
  if (!saw) throw new Error("The reply did not arrive.");
  return ended;
}

function begin(botId: string, chatId: string, seed?: { label?: string; run?: Run | null }) {
  const key = flightKey(botId, chatId);
  const previous = flights.get(key);
  if (previous) {
    previous.replaced = true;
    previous.sending = false;
    previous.control.abort();
  }
  settledRuns.delete(key);
  const flight: Flight = {
    botId,
    chatId,
    runId: "",
    control: new AbortController(),
    live: {
      phase: "thinking",
      label: (seed?.label || "").trim() || "Waiting on model",
      text: "",
      reasoning: "",
      run: seed?.run || null,
      heardAt: Date.now(),
      reasoningLive: false,
    },
    sending: true,
    replaced: false,
    settled: false,
  };
  flights.set(key, flight);
  flushNow(flight);
  return flight;
}

function placeMessage(flight: Flight, message: ChatMessage, options: { append?: boolean } = {}) {
  queryClient.setQueryData<Chat>(["chat", flight.botId, flight.chatId], (chat) => {
    if (!chat) return chat;
    const messages = [...(chat.messages || [])];
    const known = message.id ? messages.findIndex((item) => item.id === message.id) : -1;
    const byClient = message.client_id
      ? messages.findIndex((item) => item.client_id === message.client_id)
      : -1;
    if (known >= 0) {
      messages[known] = { ...messages[known], ...message };
    } else if (byClient >= 0) {
      messages[byClient] = { ...messages[byClient], ...message };
    } else if (!options.append) {
      const pending = messages.findIndex((item) => !item.id && item.role === "user" && item.content === message.content);
      if (pending >= 0) messages[pending] = { ...messages[pending], ...message };
      else messages.push(message);
    } else {
      messages.push(message);
    }
    return {
      ...chat,
      messages,
      message_count: Math.max(chat.message_count ?? messages.length, messages.length),
    };
  });
}

export const RETRY_REASON = "Not sent. Retry to send it again.";

export function optimisticUser(botId: string, chatId: string, text: string, chip?: ChatMessage["chip"], clientId?: string) {
  const client_id = clientId || (typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `c-${Date.now()}`);
  const message: ChatMessage = { role: "user", content: text, client_id, ...(chip ? { chip } : {}) };
  queryClient.setQueryData<Chat>(["chat", botId, chatId], (chat) => {
    if (!chat) return chat;
    const messages = [...(chat.messages || [])];
    const known = messages.findIndex((item) => item.client_id === client_id);
    if (known >= 0) messages[known] = { ...messages[known], ...message };
    else messages.push(message);
    return {
      ...chat,
      messages,
      message_count: Math.max(chat.message_count ?? messages.length, messages.length),
    };
  });
  return client_id;
}

function markNotSent(botId: string, chatId: string, clientId: string) {
  queryClient.setQueryData<Chat>(["chat", botId, chatId], (chat) => {
    if (!chat) return chat;
    return {
      ...chat,
      messages: (chat.messages || []).map((item) =>
        item.client_id === clientId ? { ...item, chip: "unsent", refuse_reason: RETRY_REASON } : item,
      ),
    };
  });
}

export async function sendMessage(botId: string, chatId: string, text: string, file?: File | null, clientId?: string) {
  const cached = queryClient.getQueryData<Chat>(["chat", botId, chatId]);
  const serverBusy = cached?.run?.status === "running";
  const existing = flightFor(botId, chatId);
  if (serverBusy) {
    const flight = existing && !existing.replaced ? existing : null;
    const control = flight?.control || new AbortController();
    if (flight?.live && !flight.frozen) {
      const replyId = flight.live.replyId || flight.live.run?.reply_id || "";
      flight.frozen = { replyId, text: flight.live.text || "", reasoning: flight.live.reasoning || "" };
      const step = flight.live.label || flight.live.run?.current_step || "";
      const label = isToolLabel(step) ? PAUSE_TOOL_TEXT : PAUSE_TEXT;
      remember(flight, carry(flight.live, { label, text: flight.frozen.text, reasoning: flight.frozen.reasoning }), true);
    }
    const id = optimisticUser(botId, chatId, text, "queued", clientId);
    try {
      let attachmentId: string | null = null;
      if (file) {
        const body = new FormData();
        body.append("file", file);
        const saved = await api<{ id: string }>(`/api/bots/${botId}/chats/${chatId}/files`, { method: "POST", body });
        attachmentId = saved.id;
      }
      const response = await streamPost(
        `/api/bots/${botId}/chats/${chatId}/messages`,
        { content: text, attachment_id: attachmentId, client_id: id },
        control.signal,
      );
      const type = response.headers.get("content-type") || "";
      if (type.includes("application/json")) {
        const data = (await response.json()) as { reply?: string; chat?: Chat };
        if (data.chat) {
          const prev = queryClient.getQueryData<Chat>(["chat", botId, chatId]);
          queryClient.setQueryData(["chat", botId, chatId], mergeChat(prev, data.chat));
        }
        if (flight?.live) {
          remember(flight, carry(flight.live, { aside: String(data.reply || ""), run: data.chat?.run || flight.live.run }), true);
        }
        return;
      }
      if (flight) await readEvents(response, flight);
      else await response.text();
    } catch (error) {
      if (flight?.replaced) return;
      if (error instanceof DOMException && error.name === "AbortError" && control.signal.aborted) return;
      markNotSent(botId, chatId, id);
      throw error instanceof Error ? error : new Error("The reply failed.");
    }
    return;
  }
  const flight = begin(botId, chatId);
  const id = optimisticUser(botId, chatId, text, undefined, clientId);
  try {
    let attachmentId: string | null = null;
    if (file) {
      const body = new FormData();
      body.append("file", file);
      const saved = await api<{ id: string }>(`/api/bots/${botId}/chats/${chatId}/files`, { method: "POST", body });
      attachmentId = saved.id;
    }
    const response = await streamPost(
      `/api/bots/${botId}/chats/${chatId}/messages`,
      { content: text, attachment_id: attachmentId, client_id: id },
      flight.control.signal,
    );
    const type = response.headers.get("content-type") || "";
    if (type.includes("application/json")) {
      const data = (await response.json()) as Chat & { chat?: Chat };
      finish(flight, data.chat || data);
      return;
    }
    const ended = await readEvents(response, flight);
    await afterStream(flight, botId, chatId, ended);
  } catch (error) {
    if (flight.replaced || (error instanceof DOMException && error.name === "AbortError")) return;
    flight.sending = false;
    remember(flight, {
      phase: "error",
      label: "Stopped",
      text: error instanceof Error ? error.message : "The reply failed.",
      reasoning: "",
      reasoningLive: false,
      run: flight.live?.run || null,
      heardAt: Date.now(),
    }, true);
  }
}

export async function attachLive(botId: string, chatId: string) {
  const existing = flightFor(botId, chatId);
  if (existing && !existing.replaced && existing.sending && !existing.settled) return;
  const cached = queryClient.getQueryData<Chat>(["chat", botId, chatId]);
  const flight = begin(botId, chatId, { label: cached?.run?.current_step, run: cached?.run });
  try {
    const response = await streamPost(`/api/bots/${botId}/chats/${chatId}/live`, {}, flight.control.signal);
    const ended = await readEvents(response, flight);
    await afterStream(flight, botId, chatId, ended);
  } catch (error) {
    if (flight.replaced || (error instanceof DOMException && error.name === "AbortError")) return;
    flight.sending = false;
    if (flight.live) remember(flight, carry(flight.live, { phase: "thinking" }), true);
  }
}

export async function retryMessage(botId: string, chatId: string) {
  const flight = begin(botId, chatId);
  try {
    const response = await streamPost(`/api/bots/${botId}/chats/${chatId}/retry`, {}, flight.control.signal);
    const type = response.headers.get("content-type") || "";
    if (type.includes("application/json")) {
      const data = (await response.json()) as Chat & { chat?: Chat };
      finish(flight, data.chat || data);
      return;
    }
    const ended = await readEvents(response, flight);
    await afterStream(flight, botId, chatId, ended);
  } catch (error) {
    if (flight.replaced || (error instanceof DOMException && error.name === "AbortError")) return;
    remember(flight, {
      phase: "error",
      label: "Stopped",
      text: error instanceof Error ? error.message : "The reply failed.",
      reasoning: "",
      reasoningLive: false,
      run: null,
      heardAt: Date.now(),
    }, true);
  }
}

async function afterStream(flight: Flight, botId: string, chatId: string, ended: string) {
  if (flight.replaced) return;
  if (ended === "done" || ended === "error" || ended === "stopped") {
    // finish() already stored the chat carried on the terminal event.
    if (flight.settled) return;
    let chat = queryClient.getQueryData<Chat>(["chat", botId, chatId]);
    try {
      chat = await api<Chat>(`/api/bots/${botId}/chats/${chatId}?window=80`);
    } catch {
      /* the cached transcript still leaves this run idle */
    }
    finish(flight, chat);
    return;
  }
  try {
    const chat = await api<Chat>(`/api/bots/${botId}/chats/${chatId}?window=80`);
    if (chat.run?.status === "running") {
      flight.live = flight.live || { phase: "thinking", label: chat.run.current_step || "Thinking", text: "", reasoning: "", run: chat.run, heardAt: Date.now() };
      flight.live.run = chat.run;
      flushNow(flight);
    } else {
      finish(flight, chat);
    }
  } catch {
    /* the page can retry */
  }
}

export async function sendNow(botId: string, chatId: string, messageId: string) {
  const current = queryClient.getQueryData<Chat>(["chat", botId, chatId]);
  const row = (current?.messages || []).find((item) => item.id === messageId);
  if (row?.chip === "unsent") {
    const flight = begin(botId, chatId);
    try {
      const response = await streamPost(
        `/api/bots/${botId}/chats/${chatId}/messages/${messageId}/send-now`,
        {},
        flight.control.signal,
      );
      const type = response.headers.get("content-type") || "";
      if (type.includes("application/json")) {
        const data = (await response.json()) as Chat & { chat?: Chat };
        finish(flight, data.chat || data);
        return;
      }
      const ended = await readEvents(response, flight);
      await afterStream(flight, botId, chatId, ended);
    } catch (error) {
      if (flight.replaced || (error instanceof DOMException && error.name === "AbortError")) return;
      flight.sending = false;
      remember(flight, {
        phase: "error",
        label: "Stopped",
        text: error instanceof Error ? error.message : "The reply failed.",
        reasoning: "",
        reasoningLive: false,
        run: flight.live?.run || null,
        heardAt: Date.now(),
      }, true);
    }
    return;
  }
  const chat = await api<Chat>(`/api/bots/${botId}/chats/${chatId}/messages/${messageId}/send-now`, { method: "POST" });
  const prev = queryClient.getQueryData<Chat>(["chat", botId, chatId]);
  queryClient.setQueryData(["chat", botId, chatId], mergeChat(prev, chat));
}

export async function stopAndSendStored(botId: string, chatId: string, messageId: string) {
  const flight = begin(botId, chatId);
  try {
    const response = await streamPost(
      `/api/bots/${botId}/chats/${chatId}/messages/${messageId}/stop-and-send`,
      {},
      flight.control.signal,
    );
    const type = response.headers.get("content-type") || "";
    if (type.includes("application/json")) {
      const data = (await response.json()) as Chat & { chat?: Chat };
      finish(flight, data.chat || data);
      return;
    }
    const ended = await readEvents(response, flight);
    await afterStream(flight, botId, chatId, ended);
  } catch (error) {
    if (flight.replaced || (error instanceof DOMException && error.name === "AbortError")) return;
    remember(flight, {
      phase: "error",
      label: "Stopped",
      text: error instanceof Error ? error.message : "The reply failed.",
      reasoning: "",
      reasoningLive: false,
      run: flight.live?.run || null,
      heardAt: Date.now(),
    }, true);
  }
}

export async function stopMessage(botId: string, chatId: string) {
  const flight = flightFor(botId, chatId);
  if (flight?.control) {
    flight.sending = false;
    flight.control.abort();
  }
  const chat = await api<Chat>(`/api/bots/${botId}/chats/${chatId}/stop`, { method: "POST" });
  if (flight) finish(flight, chat);
  else {
    const prev = queryClient.getQueryData<Chat>(["chat", botId, chatId]);
    queryClient.setQueryData(["chat", botId, chatId], mergeChat(prev, chat));
  }
}

export async function retryUnsent(botId: string, chatId: string, clientId: string) {
  const current = queryClient.getQueryData<Chat>(["chat", botId, chatId]);
  const row = (current?.messages || []).find((item) => item.client_id === clientId);
  if (!row) return;
  queryClient.setQueryData<Chat>(["chat", botId, chatId], (chat) =>
    chat ? { ...chat, messages: (chat.messages || []).filter((item) => item.client_id !== clientId) } : chat,
  );
  await sendMessage(botId, chatId, row.content || "", null, clientId);
}

function unansweredTurn(chat: Chat | undefined): boolean {
  const rows = chat?.messages || [];
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const message = rows[index];
    if (!message) continue;
    const kind = message.kind || "";
    if (kind === "status" || kind === "pickup" || kind === "activity") continue;
    if (message.role === "user" && !message.harness) return true;
    if (message.role !== "assistant") continue;
    const text = (message.content || "").trim();
    if (message.error || text === "Stopped." || text.startsWith("Stopped:")) return true;
    if (kind === "resume-offer" || kind === "connection") return true;
    return false;
  }
  return false;
}

export function canResume(chat: Chat | undefined, flight: Flight | null) {
  const serverBusy = chat?.run?.status === "running";
  if (serverBusy) return { retry: false, cont: false };
  const waiting = unansweredTurn(chat);
  const liveStopped = flight?.live?.phase === "stopped" || flight?.live?.phase === "error";
  const show = Boolean(waiting || (liveStopped && waiting));
  return { retry: show, cont: show };
}
