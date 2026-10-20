import { useQuery } from "@tanstack/react-query";
import { ArrowUp, MessageSquare, Mic, Plus, Square, Terminal } from "lucide-react";
import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { api, queryClient } from "@/api";
import { ApprovalCard } from "@/components/ApprovalCard";
import { Face } from "@/components/Face";
import { Markdown } from "@/components/Markdown";
import { ThinkingBox } from "@/components/ThinkingBox";
import { WindowControls } from "@/components/WindowControls";
import { moodForReaction, pokeFace } from "@/lib/mood";
import { chipText } from "@/lib/chips";
import { yoloClock, yoloRemaining } from "@/lib/yolo";
import { activityLabel, askedLine } from "@/lib/activity";
import { fitComposer, resetComposer } from "@/lib/composer";
import { activityLine, describeRun, faceStateFor, runPresence, runTone, workingLine } from "@/lib/run";
import { hideWhileSending, messageHasBubble, reactionWho } from "@/lib/transcript";
import { cn } from "@/lib/utils";
import { keepLoadedHistory, mergeChat, sameMessage, shareChat, type MessagePage } from "@/lib/window";
import { useApp } from "@/store";
import { RETRY_REASON, canResume, chatStillPolling, flightFor, retryMessage, retryUnsent, sendMessage, sendNow, settleIncoming, stopAndSendStored, stopMessage, useLive } from "@/stream";
import type { Bot, Chat, ChatMessage } from "@/types";

const REACTIONS = ["👍", "👎", "❤️", "👀"];

function AttachmentView({ message, botId, chatId }: { message: ChatMessage; botId: string; chatId: string }) {
  const att = message.attachment;
  if (!att?.id) return null;
  const url = `/api/bots/${botId}/chats/${chatId}/files/${att.id}`;
  if ((att.media_type || "").startsWith("image/")) {
    return (
      <div className="mt-2">
        <img className="max-h-80 rounded-xl" alt={att.name || "Picture"} src={url} />
        {att.path ? <a className="mt-1 block text-xs underline" href={url}>{att.path}</a> : null}
      </div>
    );
  }
  return (
    <div className="mt-2">
      {att.excerpt ? <pre className="overflow-auto rounded-xl bg-black/5 p-2 text-xs dark:bg-white/10">{att.excerpt}</pre> : null}
      <a className="text-xs underline" href={url}>{att.path || att.name || "File"}</a>
    </div>
  );
}

function Receipts({ message }: { message: ChatMessage }) {
  const [open, setOpen] = useState(false);
  const rows = message.receipts || [];
  if (message.role !== "assistant" || (!message.unverified && rows.length === 0)) return null;
  return (
    <div className="sys-line flex-col">
      {message.unverified ? <p>Unverified</p> : null}
      {rows.length ? (
        <>
          <button type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
            Receipts
          </button>
          {open ? (
            <ul className="mt-1 max-w-xl space-y-1 text-left text-[12.5px] text-muted">
              {rows.map((row, index) => (
                <li key={`${row.claim}-${index}`}>
                  <span className="font-medium text-foreground">{row.claim}</span>
                  {row.tool ? ` · ${row.tool}` : ""}
                  <span className="block whitespace-pre-wrap">{row.output || "No tool result after the last change."}</span>
                </li>
              ))}
            </ul>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function Quiet({ icon, text, detail }: { icon?: ReactNode; text: string; detail?: string }) {
  const [open, setOpen] = useState(false);
  const body = (detail || "").trim();
  if (!body) {
    return (
      <div className="sys-line">
        <p>{icon}{text}</p>
      </div>
    );
  }
  return (
    <div className="sys-line flex-col">
      <button type="button" onClick={() => setOpen((value) => !value)}>
        {icon}
        {text}
      </button>
      {open ? <p className="mt-1 max-w-xl whitespace-pre-wrap text-center text-[12.5px] text-muted">{body}</p> : null}
    </div>
  );
}

const MessageRow = memo(function MessageRow({
  message,
  last,
  botName,
  sending,
  onChoose,
}: {
  message: ChatMessage;
  last: boolean;
  botName: string;
  sending: boolean;
  onChoose: (choice: string) => void;
}) {
  const botId = useApp((state) => state.botId);
  const chatId = useApp((state) => state.chatId);
  const setScreen = useApp((state) => state.setScreen);
  if (hideWhileSending(message, sending)) return null;
  if (message.about === "activity") return null;
  if (message.kind === "resume-note" && (message.content || "").includes("Picked up where it stopped")) {
    return (
      <li className="message-row" data-kind="resume-note">
        <p className="sys-line" data-testid="pickup-card">{message.content}</p>
      </li>
    );
  }
  if (message.kind === "status" || message.kind === "pickup") {
    return (
      <li className="message-row" data-kind={message.kind}>
        <p className="sys-line" data-testid={message.kind === "pickup" ? "pickup-marker" : "status-note"}>{message.content}</p>
      </li>
    );
  }
  const failed = Boolean(message.error);
  const chip = chipText(message);
  const mine = message.role === "user";
  const showBubble = messageHasBubble(message);
  const choices = last && message.role === "assistant" && !failed && (message.choices || []).length > 1 ? message.choices || [] : [];
  const who = reactionWho(message.reaction_by, botName);
  async function react(emoji: string) {
    if (!message.id || !botId || !chatId) return;
    const removing = message.reaction === emoji;
    const chat = await api<Chat>(`/api/bots/${botId}/chats/${chatId}/messages/${message.id}/reaction`, { method: "POST", json: { emoji } });
    const prev = queryClient.getQueryData<Chat>(["chat", botId, chatId]);
    queryClient.setQueryData(["chat", botId, chatId], mergeChat(prev, chat));
    if (!removing) pokeFace(botId, moodForReaction(emoji));
  }
  return (
    <li tabIndex={message.id ? -1 : undefined} className={cn("message-row flex flex-col", mine ? "items-end" : "items-start")}>
      {message.role === "assistant" && !failed && (message.thinking || "").trim() ? <ThinkingBox text={message.thinking} seconds={message.thought_seconds} /> : null}
      {showBubble ? (
      <div className={cn(mine ? "bubble-user" : "bubble-bot", failed && "bg-danger/10 text-danger")}>
        {message.routine_name ? <p className="mb-1 text-xs opacity-70">Routine · {message.routine_name}</p> : null}
        {message.speaker_name ? <p className="mb-1 text-xs opacity-70">{message.speaker_name}</p> : null}
        <div className={mine ? "whitespace-pre-wrap" : ""}>
          {mine ? (message.content || "") : <Markdown text={message.content || ""} />}
        </div>
        {chip ? <p className="mt-2 text-xs" data-testid={`chip-${message.chip}`}>{chip}</p> : null}
        {message.chip === "held" && message.id && botId && chatId ? (
          <div className="mt-2">
            <button type="button" className="rounded-full bg-white px-3 py-1 text-sm text-foreground" data-testid="retry-held" onClick={() => void sendNow(botId, chatId, message.id || "")}>Retry</button>
          </div>
        ) : null}
        {message.chip === "unsent" && message.refuse_reason === RETRY_REASON && message.client_id && botId && chatId ? (
          <div className="mt-2">
            <button type="button" className="rounded-full bg-white px-3 py-1 text-sm text-foreground" data-testid="retry-unsent" onClick={() => void retryUnsent(botId, chatId, message.client_id || "")}>Retry</button>
          </div>
        ) : null}
        {(message.chip === "refused" || message.chip === "unsent") && message.refuse_reason !== RETRY_REASON && message.id && botId && chatId ? (
          <div className="mt-2 flex flex-wrap gap-2">
            <button type="button" className="rounded-full bg-white px-3 py-1 text-sm text-foreground" data-testid="send-now" onClick={() => void sendNow(botId, chatId, message.id || "")}>Send now</button>
            {message.chip === "refused" ? (
              <button type="button" className="rounded-full bg-white px-3 py-1 text-sm text-foreground" data-testid="stop-and-send-message" onClick={() => void stopAndSendStored(botId, chatId, message.id || "")}>Stop and send</button>
            ) : null}
          </div>
        ) : null}
        {message.skills_saved?.length ? <p className="mt-2 text-xs opacity-70">Saved skill {message.skills_saved.join(", ")}</p> : null}
        {botId && chatId ? <AttachmentView message={message} botId={botId} chatId={chatId} /> : null}
        {choices.length ? (
          <div className="mt-3 flex flex-wrap gap-2">
            {choices.map((choice) => (
              <button key={choice} type="button" className="rounded-full bg-white px-3 py-1 text-sm text-foreground" onClick={() => onChoose(choice)}>{choice}</button>
            ))}
          </div>
        ) : null}
        {message.kind === "safety-upgrade" && botId ? (
          <div className="mt-3">
            <button
              type="button"
              className="rounded-full bg-white px-3 py-1 text-sm text-foreground"
              data-testid="switch-balanced"
              onClick={() => void api(`/api/bots/${botId}/safety`, { method: "POST", json: { mode: "balanced" } }).then(() => {
                void queryClient.invalidateQueries({ queryKey: ["bots"] });
              })}
            >
              Switch back to Balanced
            </button>
          </div>
        ) : null}
        {message.kind === "safety-git-note" && botId && message.git_repo ? (
          <div className="mt-3">
            <button
              type="button"
              className="rounded-full bg-white px-3 py-1 text-sm text-foreground"
              data-testid="allow-repo"
              onClick={() => void api(`/api/bots/${botId}/safety`, { method: "POST", json: { git_allow: message.git_repo } }).then(() => {
                void queryClient.invalidateQueries({ queryKey: ["bots"] });
              })}
            >
              Always allow this repo
            </button>
          </div>
        ) : null}
        {message.kind === "connection" ? (
          <div className="mt-3">
            <button
              type="button"
              className="rounded-full bg-white px-3 py-1 text-sm text-foreground"
              data-testid="open-connection-settings"
              onClick={() => setScreen("connections")}
            >
              Connection settings
            </button>
          </div>
        ) : null}
        {message.kind === "resume-offer" && botId && chatId ? (
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              className="rounded-full bg-white px-3 py-1 text-sm text-foreground"
              data-testid="resume-job"
              onClick={() => void api<{ chat?: Chat }>(`/api/bots/${botId}/chats/${chatId}/resume`, { method: "POST" }).then((next) => {
                const saved = next.chat;
                if (!saved) return;
                const prev = queryClient.getQueryData<Chat>(["chat", botId, chatId]);
                queryClient.setQueryData(["chat", botId, chatId], mergeChat(prev, saved));
              })}
            >
              Resume
            </button>
            <button
              type="button"
              className="rounded-full bg-white px-3 py-1 text-sm text-foreground"
              data-testid="dismiss-job"
              onClick={() => void api<Chat>(`/api/bots/${botId}/chats/${chatId}/dismiss`, { method: "POST" }).then((next) => {
                const prev = queryClient.getQueryData<Chat>(["chat", botId, chatId]);
                queryClient.setQueryData(["chat", botId, chatId], mergeChat(prev, next));
              })}
            >
              Dismiss
            </button>
          </div>
        ) : null}
        {message.kind === "safety-offer" && botId ? (
          <div className="mt-3">
            <button
              type="button"
              className="rounded-full bg-white px-3 py-1 text-sm text-foreground"
              data-testid="switch-strict"
              onClick={() => void api(`/api/bots/${botId}/safety`, { method: "POST", json: { mode: "strict" } }).then(() => {
                void queryClient.invalidateQueries({ queryKey: ["bots"] });
              })}
            >
              Switch to Strict
            </button>
          </div>
        ) : null}
      </div>
      ) : null}
      {message.check === "revised" ? <Quiet text="Revised after check" /> : null}
      {message.check === "checked" ? <Quiet text="Checked" /> : null}
      {message.lesson?.startsWith("Learned:") ? <Quiet text={message.lesson} /> : null}
      {message.role === "assistant" ? <Receipts message={message} /> : null}
      {message.id ? (
        <div className="reaction-row">
          {message.reaction ? (
            <button
              type="button"
              className="reaction-pill"
              data-testid="reaction-pill"
              title={`${who} reacted`}
              aria-label={`${who} reacted ${message.reaction}`}
              onClick={() => void react(message.reaction || "")}
            >
              <span aria-hidden="true">{message.reaction}</span>
              <span className="reaction-who">{who}</span>
            </button>
          ) : null}
          <div className="reaction-picker" data-testid="reaction-picker" role="group" aria-label="React to this message">
            {REACTIONS.map((emoji) => (
              <button key={emoji} type="button" className="reaction-pick" aria-label={`React ${emoji}`} onClick={() => void react(emoji)}>{emoji}</button>
            ))}
          </div>
        </div>
      ) : null}
    </li>
  );
}, (prev, next) =>
  prev.last === next.last &&
  prev.sending === next.sending &&
  prev.botName === next.botName &&
  sameMessage(prev.message, next.message)
);

const Transcript = memo(function Transcript({
  rows,
  botName,
  sending,
  onChoose,
}: {
  rows: ChatMessage[];
  botName: string;
  sending: boolean;
  onChoose: (choice: string) => void;
}) {
  return (
    <ol className="flex flex-col gap-4">
      {rows.map((message, index) => (
        <MessageRow
          key={message.id || index}
          message={message}
          last={index === rows.length - 1}
          botName={botName}
          sending={sending}
          onChoose={onChoose}
        />
      ))}
    </ol>
  );
});

export function ChatPane() {
  const botId = useApp((state) => state.botId);
  const chatId = useApp((state) => state.chatId);
  const selectChat = useApp((state) => state.selectChat);
  const setScreen = useApp((state) => state.setScreen);
  const bots = useQuery({ queryKey: ["bots"], queryFn: () => api<Bot[]>("/api/bots") });
  const ongoing = useQuery({
    queryKey: ["ongoing", botId],
    enabled: Boolean(botId),
    queryFn: () => api<Chat>(`/api/bots/${botId}/ongoing`),
  });
  const chat = useQuery({
    queryKey: ["chat", botId, chatId],
    enabled: Boolean(botId && chatId),
    queryFn: () => api<Chat>(`/api/bots/${botId}/chats/${chatId}?window=80`),
    refetchInterval: (query) => chatStillPolling(botId || "", chatId || "", query.state.data?.run?.status, query.state.data?.run?.id),
    structuralSharing: (prev, incoming) => {
      if (!incoming || typeof incoming !== "object") return incoming;
      return shareChat(prev, keepLoadedHistory(prev, settleIncoming(botId || "", chatId || "", incoming as Chat)));
    },
  });
  const [now, setNow] = useState(() => Date.now());
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [askId, setAskId] = useState("");
  const [menu, setMenu] = useState(false);
  const [listening, setListening] = useState(false);
  const [fileName, setFileName] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const bot = (bots.data || []).find((item) => item.id === botId) || null;
  const yoloLeft = yoloRemaining(chat.data?.yolo, chat.dataUpdatedAt || now, now);
  const yoloOn = yoloLeft > 0;

  async function toggleYolo() {
    if (!botId || !chatId) return;
    try {
      const next = await api<Chat>(`/api/bots/${botId}/chats/${chatId}/yolo`, {
        method: "POST",
        json: { on: !yoloOn },
      });
      const prev = queryClient.getQueryData<Chat>(["chat", botId, chatId]);
      queryClient.setQueryData(["chat", botId, chatId], mergeChat(prev, next));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "YOLO was not changed.");
    }
  }
  const flight = botId && chatId ? flightFor(botId, chatId) : null;
  const live = useLive(botId, chatId);
  const readSent = useRef("");
  const chooseRef = useRef<(choice: string) => void>(() => undefined);
  const onChoose = useCallback((choice: string) => chooseRef.current(choice), []);

  useEffect(() => {
    const id = ongoing.data?.id;
    if (!botId || !id) return;
    if (chatId === id) return;
    selectChat(id);
  }, [botId, chatId, ongoing.data?.id, selectChat]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!botId || !chat.data?.id) return;
    const chatKey = chat.data.id;
    const through = chat.data.message_count ?? (chat.data.messages || []).length;
    const token = `${botId}:${chatKey}:${through}`;
    if (readSent.current === token) return;
    const timer = window.setTimeout(() => {
      if (readSent.current === token) return;
      readSent.current = token;
      void api(`/api/bots/${botId}/chats/${chatKey}/read`, {
        method: "POST",
        json: { through },
      }).then(() => queryClient.invalidateQueries({ queryKey: ["unread"] })).catch(() => undefined);
    }, 500);
    return () => window.clearTimeout(timer);
  }, [botId, chat.data?.id, chat.data?.message_count]);

  const stick = useRef(true);
  const anchor = useRef<{ height: number; top: number } | null>(null);
  const loadingOlder = useRef(false);
  const pinning = useRef(false);
  const messageCount = chat.data?.messages.length || 0;

  function onScroll() {
    const el = scroller.current;
    if (!el || pinning.current) return;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (el.scrollTop < 64) void loadOlder();
  }

  async function loadOlder() {
    if (loadingOlder.current || !botId || !chatId) return;
    const start = chat.data?.window_start ?? 0;
    if (start <= 0) return;
    loadingOlder.current = true;
    stick.current = false;
    try {
      const page = await api<MessagePage>(`/api/bots/${botId}/chats/${chatId}/messages?before=${start}&limit=80`);
      if (!page.messages.length || page.start >= start) return;
      const el = scroller.current;
      if (el) anchor.current = { height: el.scrollHeight, top: el.scrollTop };
      queryClient.setQueryData<Chat>(["chat", botId, chatId], (current) => {
        if (!current) return current;
        return {
          ...current,
          messages: [...page.messages, ...(current.messages || [])],
          window_start: page.start,
          message_count: page.total,
        };
      });
    } catch {
      anchor.current = null;
    } finally {
      loadingOlder.current = false;
    }
  }

  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const pin = (top: number) => {
      pinning.current = true;
      el.scrollTop = top;
      pinning.current = false;
    };
    if (anchor.current) {
      pin(anchor.current.top + (el.scrollHeight - anchor.current.height));
      anchor.current = null;
      return;
    }
    if (!stick.current) return;
    pin(el.scrollHeight);
  }, [messageCount, live?.text, live?.reasoning, live?.label]);

  useEffect(() => {
    const el = scroller.current;
    if (!el || !chat.data) return;
    if ((chat.data.window_start ?? 0) > 0 && el.scrollHeight <= el.clientHeight + 8) void loadOlder();
  }, [chat.data?.window_start, messageCount]);

  useEffect(() => {
    box.current?.focus();
  }, [botId, chatId]);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    if (!draft) resetComposer(el);
    else fitComposer(el);
  }, [draft]);

  useEffect(() => {
    setMenu(false);
  }, [botId]);

  const faceFor = (sending: boolean, phase?: string, label?: string, text?: string, status?: string, step?: string) => faceStateFor({
    sending, phase, label, text, runStatus: status, step,
  });

  if (bots.isLoading || (bots.data || []).length > 0) {
    if (!bot) return <p className="p-6 text-sm text-muted">Loading bots…</p>;
  }
  if (!bot) {
    return (
      <section className="relative flex h-full min-h-0 flex-col">
        <div className="title-drag absolute inset-x-0 top-0 h-10" data-tauri-drag-region />
        <WindowControls />
        <div className="flex flex-1 flex-col items-center justify-center px-6 text-center">
          <img src="/static/mascot.svg" alt="" width={168} height={168} />
          <h1 className="mt-4 text-3xl font-semibold tracking-tight">Add a bot.</h1>
          <p className="pixel mt-3 text-2xl">AI agents, made easy.</p>
        </div>
      </section>
    );
  }

  const open = chat.data;
  const resume = open ? canResume(open, flight) : { retry: false, cont: false };
  const serverSettled = open?.run?.status === "idle" || open?.run?.status === "stopped" || open?.run?.status === "error";
  const running = Boolean(open && (open.run?.status === "running" || (flight?.sending && live && !serverSettled && live.phase !== "error" && live.phase !== "stopped")));
  const step = live?.label || open?.run?.current_step || "Thinking";
  const view = open ? describeRun(step, open.run?.status || (flight?.sending ? "running" : "idle"), live?.run?.started_at || open.run?.started_at, live?.run?.last_activity_at || open.run?.last_activity_at, live?.heardAt, now) : null;
  const tone = view ? runTone(view.words, running ? "running" : open?.run?.status || "idle") : "thinking";
  const face = faceFor(Boolean(flight?.sending), live?.phase, view?.words, live?.text, running ? "running" : open?.run?.status, view?.words);
  const asking = (open?.messages || []).slice(-1)[0]?.choices;
  const rows = open?.messages || [];
  const steps = live?.steps || [];
  const asked = askedLine(bot.endpoint_name || "", rows);
  const pill = workingLine(live?.run || open?.run, now);
  const activity = activityLine({
    step: live?.label || open?.run?.current_step || "",
    jobStartedAt: live?.run?.started_at || open?.run?.started_at,
    activityStartedAt: live?.waitAt ? new Date(live.waitAt).toISOString() : (live?.run?.activity_started_at || open?.run?.activity_started_at),
    now,
    tokens: Math.floor((live?.chars || 0) / 4),
  });
  const checklist = (open?.plan?.steps || []).filter((step) => step && step.text);
  const active = Boolean(open && (running || (flight?.sending && live && live.phase !== "error" && live.phase !== "stopped")));
  const presenceStep = live?.label || open?.run?.current_step || "";
  const presenceWaiting = !presenceStep || /^(Thinking|Waiting|Revising)\b/.test(presenceStep) || presenceStep.startsWith("Waiting on model") || presenceStep.startsWith("Model not answering");
  const presenceStarted = presenceWaiting
    ? (live?.waitAt ? new Date(live.waitAt).toISOString() : (open?.run?.activity_started_at || open?.run?.started_at || live?.run?.started_at))
    : (open?.run?.activity_started_at || live?.run?.activity_started_at || open?.run?.started_at || live?.run?.started_at);
  const presence = runPresence({ active, step: presenceStep, startedAt: presenceStarted, now });

  chooseRef.current = (choice) => {
    void submit(choice);
  };

  async function submit(text: string) {
    if (!botId || !chatId) return;
    const file = fileRef.current?.files?.[0] || null;
    if (!text && !file) return;
    setError("");
    setDraft("");
    setFileName("");
    if (fileRef.current) fileRef.current.value = "";
    try {
      if (typeof Notification !== "undefined" && Notification.permission === "default") void Notification.requestPermission();
      await sendMessage(botId, chatId, text, file);
    } catch (reason) {
      setDraft((current) => current || text);
      setError(reason instanceof Error ? reason.message : "The reply failed.");
    }
  }

  function listen() {
    const Speech = (window as Window & { SpeechRecognition?: new () => { lang: string; onresult: ((event: { results: { 0: { 0: { transcript: string } } } }) => void) | null; onend: (() => void) | null; start: () => void }; webkitSpeechRecognition?: new () => { lang: string; onresult: ((event: { results: { 0: { 0: { transcript: string } } } }) => void) | null; onend: (() => void) | null; start: () => void } }).SpeechRecognition
      || (window as Window & { webkitSpeechRecognition?: new () => { lang: string; onresult: ((event: { results: { 0: { 0: { transcript: string } } } }) => void) | null; onend: (() => void) | null; start: () => void } }).webkitSpeechRecognition;
    if (!Speech) {
      setError("This browser cannot use the microphone.");
      return;
    }
    const ear = new Speech();
    ear.lang = "en-US";
    ear.onresult = (event) => setDraft(event.results[0][0].transcript);
    ear.onend = () => setListening(false);
    setListening(true);
    ear.start();
  }

  return (
    <section className="relative flex h-full min-h-0 flex-1 flex-col">
      <div className="title-drag relative z-10 flex h-14 shrink-0 items-center justify-center" data-tauri-drag-region>
      <WindowControls />
      <div className="relative z-20 flex items-center justify-center gap-2" data-testid="bot-pill">
        <div className="relative">
          <button
            type="button"
            aria-label={`${bot.name} menu`}
            onClick={() => setMenu((openMenu) => !openMenu)}
            className="flex items-center gap-2 rounded-full bg-background px-3 py-1.5 shadow-[0_1px_2px_rgba(0,0,0,0.06),0_8px_24px_rgba(0,0,0,0.06)]"
          >
            <Face color={bot.face_color} state={face} botId={bot.id} tiny />
            <h1 className="pixel text-[16px]">{bot.name}</h1>
          </button>
          {menu ? (
            <div className="absolute left-1/2 top-11 w-64 -translate-x-1/2 rounded-2xl bg-background p-2 shadow-[0_8px_30px_rgba(0,0,0,0.12)]">
              <button type="button" className="mt-1 block w-full rounded-xl px-3 py-2 text-left text-sm hover:bg-black/5 dark:hover:bg-white/10" onClick={() => { setMenu(false); setScreen("settings"); }}>
                This bot's settings
              </button>
              <label className="mt-1 block px-3 py-1 text-xs text-muted">Ask one bot
                <select className="mt-1 h-8 w-full rounded-lg border border-border bg-card px-2 text-sm" value={askId} onChange={(event) => setAskId(event.target.value)}>
                  <option value="">Choose</option>
                  {(bots.data || []).filter((item) => item.id !== bot.id).map((item) => (
                    <option key={item.id} value={item.id}>{item.name}</option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="block w-full rounded-xl px-3 py-2 text-left text-sm hover:bg-black/5 dark:hover:bg-white/10"
                onClick={() => {
                  if (!askId || !draft.trim() || !botId || !chatId) return;
                  const task = draft.trim();
                  setDraft("");
                  setMenu(false);
                  void api(`/api/bots/${botId}/chats/${chatId}/ask`, { method: "POST", json: { bot_id: askId, task } })
                    .then((next) => {
                      const prev = queryClient.getQueryData<Chat>(["chat", botId, chatId]);
                      queryClient.setQueryData(["chat", botId, chatId], mergeChat(prev, next as Chat));
                      void queryClient.invalidateQueries({ queryKey: ["chats"] });
                    })
                    .catch((reason: Error) => setError(reason.message));
                }}
              >
                Ask
              </button>
            </div>
          ) : null}
        </div>
        {botId && chatId ? (
          <button
            type="button"
            data-testid="yolo-badge"
            aria-pressed={yoloOn}
            aria-label={yoloOn ? `YOLO on, ${yoloClock(yoloLeft)} left` : "Turn YOLO on for this chat"}
            onClick={() => void toggleYolo()}
            className={yoloOn
              ? "rounded-full bg-[#b42318] px-2.5 py-1 text-xs font-bold tracking-wide text-white"
              : "rounded-full bg-background px-2.5 py-1 text-xs font-semibold text-[#b42318] shadow-[inset_0_0_0_1px_#b42318]"}
          >
            {yoloOn ? `YOLO ${yoloClock(yoloLeft)}` : "YOLO"}
          </button>
        ) : null}
      </div>
      </div>
      {yoloOn ? (
        <div
          data-testid="yolo-banner"
          role="status"
          className="flex shrink-0 items-center justify-between gap-3 bg-[#b42318] px-4 py-2 text-sm font-semibold text-white"
        >
          <span>YOLO ON</span>
          <span className="font-normal">{yoloClock(yoloLeft)} left in this chat</span>
          <button
            type="button"
            data-testid="yolo-off"
            className="rounded-full bg-white px-3 py-1 text-xs font-bold text-[#b42318]"
            onClick={() => void toggleYolo()}
          >
            Turn off
          </button>
        </div>
      ) : null}

      <div ref={scroller} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto px-4 pb-4 pt-2" style={{ overflowAnchor: "none" }}>
        <div className="mx-auto flex w-full max-w-[760px] flex-col gap-4">
          {!chatId || !open ? (
            <div className="flex flex-1 flex-col items-center justify-center px-6 py-24 text-center">
              <img src="/static/mascot.svg" alt="" width={120} height={120} />
              <h2 className="mt-3 text-xl font-semibold">No chat open</h2>
              <p className="pixel mt-3 text-2xl">AI agents, made easy.</p>
            </div>
          ) : (
            <>
              {(open.messages || []).length === 0 && !live ? (
                <div className="flex flex-col items-center px-6 py-16 text-center">
                  <img src="/static/mascot.svg" alt="" width={140} height={140} />
                  <p className="pixel mt-4 text-2xl">AI agents, made easy.</p>
                </div>
              ) : null}
              {checklist.length ? (
                <ol data-testid="plan-checklist" className="rounded-lg border border-border bg-card px-4 py-3 text-sm">
                  {open?.plan?.title ? <li className="mb-1 list-none font-medium">{open.plan.title}</li> : null}
                  {checklist.map((step) => (
                    <li key={step.id} data-state={step.state} className={step.state === "done" ? "text-muted line-through" : step.state === "doing" ? "font-medium" : ""}>
                      {step.text}
                    </li>
                  ))}
                </ol>
              ) : null}
              {(open.window_start ?? 0) > 0 ? (
                <button type="button" className="sys-line" onClick={() => void loadOlder()}>Earlier messages</button>
              ) : null}
              <Transcript
                rows={rows}
                botName={bot?.name || "the bot"}
                sending={Boolean(flight?.sending)}
                onChoose={onChoose}
              />
              {steps.map((item) => (
                <div key={item.id} data-testid="activity-step" data-state={item.state} data-step-id={item.id}>
                  <Quiet
                    icon={runTone(item.text, "running") === "tool" ? <Terminal className="h-3 w-3" /> : <MessageSquare className="h-3 w-3" />}
                    text={activityLabel(item)}
                  />
                </div>
              ))}
              {live && (live.reasoning || "").trim() && live.phase !== "error" && live.phase !== "stopped" ? (
                <ThinkingBox text={live.reasoning} streaming={Boolean(live.reasoningLive)} seconds={live.thoughtSeconds} startedAt={live.thoughtAt} now={now} />
              ) : null}
              {live && live.phase !== "error" && live.phase !== "stopped" && (live.text || "").trim() ? (
                <div className="bubble-bot">
                  <Markdown text={live.text} />
                </div>
              ) : null}
              {running && view ? (
                <div className="sys-line" role="status" data-testid="chat-working-pill">
                  <p>
                    <span className={cn("h-1.5 w-1.5 rounded-full", tone === "halted" ? "bg-danger" : "bg-current")} />
                    <span data-testid="activity-line">{activity || pill.text || view.words}</span>
                    {pill.stuck ? <span>No activity for a while</span> : null}
                    {pill.queued ? <span data-testid="queued-badge">Queued</span> : null}
                  </p>
                </div>
              ) : null}
              {live && (live.phase === "stopped" || live.phase === "error") ? (
                <div className="sys-line" role="status"><p className="text-danger">{live.text.startsWith("Stopped") ? live.text : `Stopped: ${live.text}`}</p></div>
              ) : null}
              {open.messages?.length ? (
                <div data-testid="asked-line">
                  <Quiet icon={<MessageSquare className="h-3 w-3" />} text={asked} />
                </div>
              ) : null}
            </>
          )}
        </div>
      </div>

      <div className="shrink-0 px-4">
        {botId && bot ? <ApprovalCard botId={botId} botName={bot.name} active={running} /> : null}
      </div>
      <form
        className="composer-dock shrink-0 px-4 pb-5 pt-1"
        onSubmit={(event) => {
          event.preventDefault();
          void submit(draft.trim());
        }}
      >
        <div className="mx-auto w-full max-w-[760px]">
          {error || (live?.phase === "error" ? live.text : "") ? <p className="mb-2 text-center text-sm text-danger" role="alert">{error || live?.text}</p> : null}
          {fileName ? <p className="mb-1 text-center text-xs text-muted">{fileName}</p> : null}
          <div className="mb-2 flex justify-center gap-3 text-xs text-muted">
            {resume.retry ? <button type="button" onClick={() => botId && chatId && void retryMessage(botId, chatId)}>Retry</button> : null}
            {resume.cont ? <button type="button" onClick={() => void submit("Continue")}>Continue</button> : null}
            {running ? (
              <button type="button" className="inline-flex items-center gap-1" onClick={() => botId && chatId && void stopMessage(botId, chatId)}>
                <Square className="h-3 w-3" /> Stop
              </button>
            ) : null}
          </div>
          {running ? <p className="mb-1 text-center text-xs text-muted">Add to the current job</p> : null}
          <p className="mb-1 text-center text-xs text-muted" role="status" data-testid="run-state" data-state={presence.kind}>{presence.text}</p>
          <div className="composer">
            <button type="button" className="mb-1 flex h-8 w-8 items-center justify-center rounded-full text-foreground" aria-label="Attach" onClick={() => fileRef.current?.click()}>
              <Plus className="h-5 w-5" />
            </button>
            <input ref={fileRef} className="hidden" type="file" onChange={(event) => setFileName(event.target.files?.[0]?.name || "")} />
            <label className="sr-only" htmlFor="draft">{running ? "Add to the current job" : "Message"}</label>
            <textarea
              id="draft"
              ref={box}
              rows={1}
              value={draft}
              placeholder={listening ? "Listening" : running ? "Add to the current job" : asking && asking.length > 1 ? "Or type an answer" : "Message"}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void submit(draft.trim());
                }
              }}
            />
            <button type="button" className={cn("mb-1 flex h-8 w-8 items-center justify-center rounded-full text-muted", listening && "text-foreground")} aria-label="Microphone" onClick={listen}>
              <Mic className="h-4 w-4" />
            </button>
            {running ? (
              <button
                type="button"
                className="mb-1 rounded-full px-3 text-sm"
                data-testid="stop-and-send"
                disabled={!draft.trim() && !fileName}
                onClick={() => {
                  if (!botId || !chatId) return;
                  const text = draft.trim();
                  const file = fileRef.current?.files?.[0] || null;
                  if (!text && !file) return;
                  setDraft("");
                  setFileName("");
                  if (fileRef.current) fileRef.current.value = "";
                  void stopMessage(botId, chatId)
                    .then(() => sendMessage(botId, chatId, text, file))
                    .catch((reason: Error) => {
                      setDraft((current) => current || text);
                      setError(reason?.message || "The reply failed.");
                    });
                }}
              >
                Stop and send
              </button>
            ) : null}
            <button type="submit" className="send-btn" aria-label="Send" disabled={!draft.trim() && !fileName}>
              <ArrowUp className="h-4 w-4" />
            </button>
          </div>
        </div>
      </form>
    </section>
  );
}
