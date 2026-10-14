import { api, queryClient, streamPost } from "@/api";
import { activityLine } from "@/lib/run";
import { mergeChat } from "@/lib/window";
import { moodForBadge, pokeFace } from "@/lib/mood";
import { useApp } from "@/store";
import type { Chat, ChatMessage, Run } from "@/types";

export type Live = {
  phase: "thinking" | "reply" | "stopped" | "error";
  label: string;
  text: string;
  reasoning: string;
  run: Run | null;
  heardAt: number;
  steps?: string[];
  thoughtAt?: number;
  thoughtSeconds?: number;
  reasoningLive?: boolean;
  aside?: string;
  chars?: number;
  waitAt?: number;
};

type Flight = {
  botId: string;
  chatId: string;
  runId: string;
  control: AbortController;
  live: Live | null;
  sending: boolean;
  replaced: boolean;
  settled: boolean;
};

const flights = new Map<string, Flight>();
const settledRuns = new Map<string, { runId: string; at: number }>();

export function flightKey(botId: string, chatId: string) {
  return `${botId}:${chatId}`;
}

export function flightFor(botId: string, chatId: string): Flight | null {
  return flights.get(flightKey(botId, chatId)) || null;
}

export function resetStreamForTests() {
  for (const flight of flights.values()) flight.control.abort();
  flights.clear();
  settledRuns.clear();
}

function releaseRun(chat: Chat): Chat {
  if (chat.run?.status !== "running") return chat;
  return {
    ...chat,
    run: { id: "", status: "idle", started_at: null, last_activity_at: null, current_step: "", reason: "" },
  };
}

/** A run we already finished must not keep the transcript polling. */
export function runIsFinished(botId: string, chatId: string, status?: string | null, runId?: string | null): boolean {
  const done = settledRuns.get(flightKey(botId, chatId));
  if (!done) return false;
  if (status !== "running") return true;
  const same = !runId || !done.runId || runId === done.runId;
  if (same || Date.now() - done.at < 30_000) return true;
  settledRuns.delete(flightKey(botId, chatId));
  return false;
}

export function chatStillPolling(botId: string, chatId: string, status?: string | null, runId?: string | null): number | false {
  if (runIsFinished(botId, chatId, status, runId)) return false;
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
  useApp.getState().bump();
  if (!visible(flight) || !flight.live) return;
  queryClient.setQueryData<Chat>(["chat", flight.botId, flight.chatId], (chat) =>
    chat && flight.live?.run ? { ...chat, run: flight.live.run } : chat,
  );
}

function eventMatches(event: Record<string, unknown>, flight: Flight) {
  if (event.bot_id && event.bot_id !== flight.botId) return false;
  if (event.chat_id && event.chat_id !== flight.chatId) return false;
  const run = event.run as Run | undefined;
  const runId = String(event.run_id || run?.id || "");
  if (runId) {
    if (!flight.runId) flight.runId = runId;
    else if (runId !== flight.runId) return false;
  }
  return true;
}

function remember(flight: Flight, live: Live) {
  flight.live = live;
  paint(flight);
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
  };
}

function freezeThought(live: Live): number | undefined {
  if (live.thoughtSeconds) return live.thoughtSeconds;
  if (!live.thoughtAt) return undefined;
  return Math.max(1, Math.round((Date.now() - live.thoughtAt) / 1000));
}

function finish(flight: Flight, chat?: Chat) {
  const runId = flight.runId || flight.live?.run?.id || chat?.run?.id || "";
  settledRuns.set(flightKey(flight.botId, flight.chatId), { runId, at: Date.now() });
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
  paint(flight);
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
    remember(flight, carry(live, { label: run.current_step || live.label, run }));
    return;
  }
  if (event.type === "stopped") {
    finish(flight, event.chat as Chat | undefined);
    return;
  }
  if (event.type === "user" && event.message && typeof event.message === "object") {
    placeMessage(flight, event.message as ChatMessage);
    return;
  }
  if (event.type === "note" && event.message && typeof event.message === "object") {
    placeMessage(flight, event.message as ChatMessage, { append: true });
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
    const prev = live.steps || [];
    const steps = prev[prev.length - 1] === label ? prev : [...prev, label].slice(-8);
    const modelWait = label === "Thinking" || label.startsWith("Waiting on model");
    remember(flight, carry(live, {
      phase: "thinking",
      label,
      run,
      steps,
      chars: modelWait ? live.chars : 0,
      waitAt: modelWait ? live.waitAt || Date.now() : Date.now(),
    }));
    return;
  }
  if (event.type === "replay") {
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
    const label = activityLine({
      step: "Waiting on model",
      activityStartedAt: new Date(waitAt).toISOString(),
      now: Date.now(),
      tokens: Math.floor(chars / 4),
    });
    remember(flight, carry(live, {
      phase: live.phase === "reply" ? "reply" : "thinking",
      label,
      reasoning: live.reasoning + chunk,
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
    const label = activityLine({
      step: "Waiting on model",
      activityStartedAt: new Date(waitAt).toISOString(),
      now: Date.now(),
      tokens: Math.floor(chars / 4),
    });
    remember(flight, carry(live, {
      phase: "reply",
      label,
      text: live.text + chunk,
      reasoningLive: false,
      thoughtSeconds: freezeThought(live),
      chars,
      waitAt,
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
    remember(flight, carry(live, { phase: "error", label: "Stopped", text: String(event.detail || "The reply failed."), reasoning: "", reasoningLive: false }));
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

function begin(botId: string, chatId: string) {
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
    live: { phase: "thinking", label: "Waiting on model", text: "", reasoning: "", run: null, heardAt: Date.now(), reasoningLive: false },
    sending: true,
    replaced: false,
    settled: false,
  };
  flights.set(key, flight);
  paint(flight);
  return flight;
}

function placeMessage(flight: Flight, message: ChatMessage, options: { append?: boolean } = {}) {
  queryClient.setQueryData<Chat>(["chat", flight.botId, flight.chatId], (chat) => {
    if (!chat) return chat;
    const messages = [...(chat.messages || [])];
    const known = message.id ? messages.findIndex((item) => item.id === message.id) : -1;
    if (known >= 0) {
      messages[known] = { ...messages[known], ...message };
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

export function optimisticUser(botId: string, chatId: string, text: string, chip?: ChatMessage["chip"]) {
  const message: ChatMessage = chip ? { role: "user", content: text, chip } : { role: "user", content: text };
  queryClient.setQueryData<Chat>(["chat", botId, chatId], (chat) =>
    chat
      ? {
          ...chat,
          messages: [...(chat.messages || []), message],
          message_count: (chat.message_count ?? chat.messages.length) + 1,
        }
      : chat,
  );
}

export async function sendMessage(botId: string, chatId: string, text: string, file?: File | null) {
  const existing = flightFor(botId, chatId);
  if (existing?.sending && !existing.replaced) {
    optimisticUser(botId, chatId, text, "queued");
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
        { content: text, attachment_id: attachmentId },
        existing.control.signal,
      );
      const type = response.headers.get("content-type") || "";
      if (type.includes("application/json")) {
        const data = (await response.json()) as { reply?: string; chat?: Chat };
        if (data.chat) {
          const prev = queryClient.getQueryData<Chat>(["chat", botId, chatId]);
          queryClient.setQueryData(["chat", botId, chatId], mergeChat(prev, data.chat));
        }
        if (existing.live) {
          remember(existing, carry(existing.live, { aside: String(data.reply || ""), run: data.chat?.run || existing.live.run }));
        }
        return;
      }
      await readEvents(response, existing);
    } catch (error) {
      if (existing.replaced || (error instanceof DOMException && error.name === "AbortError")) return;
    }
    return;
  }
  const flight = begin(botId, chatId);
  optimisticUser(botId, chatId, text);
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
      { content: text, attachment_id: attachmentId },
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
    });
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
    });
  }
}

async function afterStream(flight: Flight, botId: string, chatId: string, ended: string) {
  if (flight.replaced) return;
  if (ended === "done" || ended === "error" || ended === "stopped") {
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
      paint(flight);
    } else {
      finish(flight, chat);
    }
  } catch {
    /* the page can retry */
  }
}

export async function sendNow(botId: string, chatId: string, messageId: string) {
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
    });
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

export function canResume(chat: Chat | undefined, flight: Flight | null) {
  if (flight?.sending && flight.live && flight.live.phase !== "stopped" && flight.live.phase !== "error") {
    return { retry: false, cont: false };
  }
  const run = chat?.run;
  const last = chat?.messages?.slice(-1)[0];
  const text = (last?.content || "").trim();
  const stoppedMsg = Boolean(last && last.role === "assistant" && (last.error || text === "Stopped." || text.startsWith("Stopped:")));
  const waitingUser = Boolean(last && last.role === "user" && !flight?.live);
  const runBad = run?.status === "stopped" || run?.status === "error";
  const liveStopped = flight?.live?.phase === "stopped" || flight?.live?.phase === "error";
  return {
    retry: Boolean(liveStopped || stoppedMsg || waitingUser || runBad),
    cont: Boolean(liveStopped || stoppedMsg || runBad),
  };
}
