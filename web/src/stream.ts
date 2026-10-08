import { api, queryClient, streamPost } from "@/api";
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
};

type Flight = {
  botId: string;
  chatId: string;
  runId: string;
  control: AbortController;
  live: Live | null;
  sending: boolean;
  replaced: boolean;
};

const flights = new Map<string, Flight>();

export function flightKey(botId: string, chatId: string) {
  return `${botId}:${chatId}`;
}

export function flightFor(botId: string, chatId: string): Flight | null {
  return flights.get(flightKey(botId, chatId)) || null;
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

function finish(flight: Flight, chat?: Chat) {
  flight.sending = false;
  flight.live = null;
  if (chat) {
    const prev = queryClient.getQueryData<Chat>(["chat", flight.botId, flight.chatId]);
    queryClient.setQueryData(["chat", flight.botId, flight.chatId], mergeChat(prev, chat));
    void queryClient.invalidateQueries({ queryKey: ["chats", flight.botId] });
    void queryClient.invalidateQueries({ queryKey: ["unread"] });
  }
  paint(flight);
  notifyFinished(flight, chat);
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
    remember(flight, { ...live, label: run.current_step || live.label, run, heardAt: Date.now() });
    return;
  }
  if (event.type === "stopped") {
    finish(flight, event.chat as Chat | undefined);
    return;
  }
  if (event.type === "status") {
    const run = (event.run as Run | undefined) || live.run;
    const label = String(event.text || "Thinking");
    if (run && event.text) run.current_step = label;
    const prev = live.steps || [];
    const steps = prev[prev.length - 1] === label ? prev : [...prev, label].slice(-8);
    remember(flight, { phase: "thinking", label, text: live.text, reasoning: live.reasoning, run, heardAt: Date.now(), steps });
    return;
  }
  if (event.type === "replay") {
    remember(flight, { phase: "thinking", label: live.label || "Model not answering, retrying…", text: live.text, reasoning: String(event.text || ""), run: live.run, heardAt: Date.now() });
    return;
  }
  if (event.type === "replace") {
    remember(flight, { phase: "thinking", label: "Searching", text: "", reasoning: live.reasoning, run: live.run, heardAt: Date.now() });
    return;
  }
  if (event.type === "line") {
    remember(flight, { phase: "thinking", label: live.label || "Thinking", text: live.text + String(event.text || ""), reasoning: live.reasoning, run: live.run, heardAt: Date.now() });
    return;
  }
  if (event.type === "thinking") {
    const label = !live.label || live.label === "Waiting on model" ? "Thinking" : live.label;
    remember(flight, {
      phase: live.phase === "reply" ? "reply" : "thinking",
      label,
      text: live.text,
      reasoning: live.reasoning + String(event.text || ""),
      run: live.run,
      heardAt: Date.now(),
    });
    return;
  }
  if (event.type === "delta") {
    remember(flight, { phase: "reply", label: live.label, text: live.text + String(event.text || ""), reasoning: live.reasoning, run: live.run, heardAt: Date.now() });
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
    remember(flight, { phase: "error", label: "Stopped", text: String(event.detail || "The reply failed."), reasoning: "", run: live.run, heardAt: Date.now() });
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
  const flight: Flight = {
    botId,
    chatId,
    runId: "",
    control: new AbortController(),
    live: { phase: "thinking", label: "Waiting on model", text: "", reasoning: "", run: null, heardAt: Date.now() },
    sending: true,
    replaced: false,
  };
  flights.set(key, flight);
  paint(flight);
  return flight;
}

export function optimisticUser(botId: string, chatId: string, text: string) {
  const message: ChatMessage = { role: "user", content: text };
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
    if (ended !== "done" && ended !== "error" && ended !== "stopped") {
      const chat = await api<Chat>(`/api/bots/${botId}/chats/${chatId}?window=80`);
      if (chat.run?.status === "running") {
        flight.live = flight.live || { phase: "thinking", label: chat.run.current_step || "Thinking", text: "", reasoning: "", run: chat.run, heardAt: Date.now() };
        flight.live.run = chat.run;
        paint(flight);
      } else {
        finish(flight, chat);
      }
    }
  } catch (error) {
    if (flight.replaced || (error instanceof DOMException && error.name === "AbortError")) return;
    flight.sending = false;
    remember(flight, {
      phase: "error",
      label: "Stopped",
      text: error instanceof Error ? error.message : "The reply failed.",
      reasoning: "",
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
    await readEvents(response, flight);
  } catch (error) {
    if (flight.replaced || (error instanceof DOMException && error.name === "AbortError")) return;
    remember(flight, {
      phase: "error",
      label: "Stopped",
      text: error instanceof Error ? error.message : "The reply failed.",
      reasoning: "",
      run: null,
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
