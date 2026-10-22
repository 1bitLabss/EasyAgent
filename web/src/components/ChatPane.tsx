import { useQuery } from "@tanstack/react-query";
import { MessageSquare, Terminal } from "lucide-react";
import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useStickToBottom } from "use-stick-to-bottom";
import { api, queryClient } from "@/api";
import { ApprovalCard } from "@/components/ApprovalCard";
import { Composer, type ComposerHandle } from "@/components/Composer";
import { Elapsed } from "@/components/Elapsed";
import { Face } from "@/components/Face";
import { Markdown } from "@/components/Markdown";
import { ThinkingBox } from "@/components/ThinkingBox";
import { WindowControls } from "@/components/WindowControls";
import { moodForReaction, pokeFace } from "@/lib/mood";
import { chipText, chipTitle } from "@/lib/chips";
import { yoloClock, yoloRemaining } from "@/lib/yolo";
import { activityLabel, askedLine, type ActivityStep } from "@/lib/activity";
import { deriveUserHistory } from "@/lib/composer-history";
import { activityLine, describeRun, faceStateFor, runPresence, runTone, shownStep, workingLine } from "@/lib/run";
import { arrangeBusy, hideWhileSending, messageHasBubble, placeLiveReply, reactionWho, rowKey, shownThinking } from "@/lib/transcript";
import type { FrozenReply } from "@/stream";
import { cn } from "@/lib/utils";
import { keepLoadedHistory, mergeChat, sameMessage, shareChat, type MessagePage } from "@/lib/window";
import { useApp } from "@/store";
import { RETRY_REASON, attachLive, canResume, chatStillPolling, flightFor, retryMessage, retryUnsent, sendMessage, sendNow, settleIncoming, stopAndSendStored, stopMessage, useLive } from "@/stream";
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
  streaming = false,
  freeze = false,
  pauseNote = false,
  onChoose,
}: {
  message: ChatMessage;
  last: boolean;
  botName: string;
  sending: boolean;
  streaming?: boolean;
  freeze?: boolean;
  pauseNote?: boolean;
  onChoose: (choice: string) => void;
}) {
  const botId = useApp((state) => state.botId);
  const chatId = useApp((state) => state.chatId);
  const setScreen = useApp((state) => state.setScreen);
  // An empty live placeholder can wait. A reply that already has words stays put
  // while the next message is sent, or the row above it jumps.
  if (hideWhileSending(message, sending) && !freeze && !messageHasBubble(message)) return null;
  if (message.about === "activity" || message.about === "queue" || message.id === "live-status" || message.id === "activity") return null;
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
    <li
      tabIndex={message.id ? -1 : undefined}
      className={cn("message-row flex flex-col", mine ? "items-end" : "items-start")}
      data-role={message.role}
      data-message-id={message.id || message.client_id || ""}
    >
      {message.role === "assistant" && !failed && shownThinking(message.thinking) ? <ThinkingBox text={shownThinking(message.thinking)} streaming={streaming && !freeze} seconds={message.thought_seconds} /> : null}
      {showBubble ? (
      <div className={cn(mine ? "bubble-user" : "bubble-bot", failed && "bg-danger/10 text-danger")}>
        {message.routine_name ? <p className="mb-1 text-xs opacity-70">Routine · {message.routine_name}</p> : null}
        {message.speaker_name ? <p className="mb-1 text-xs opacity-70">{message.speaker_name}</p> : null}
        <div className={mine ? "whitespace-pre-wrap" : ""}>
          {mine ? (message.content || "") : <Markdown text={message.content || ""} streaming={streaming || freeze} freeze={freeze} />}
        </div>
        {chip ? <p className="mt-2 block truncate text-xs" data-testid={`chip-${message.chip}`} title={chipTitle(message)}>{chip}</p> : null}
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
      {showBubble && (mine || message.role === "assistant") ? (
        <div className="reaction-row">
          {message.id && message.reaction ? (
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
          {message.id ? (
          <div className="reaction-picker" data-testid="reaction-picker" role="group" aria-label="React to this message">
            {REACTIONS.map((emoji) => (
              <button key={emoji} type="button" className="reaction-pick" aria-label={`React ${emoji}`} onClick={() => void react(emoji)}>{emoji}</button>
            ))}
          </div>
          ) : null}
        </div>
      ) : null}
      {(message.role === "assistant" && message.interrupted) || pauseNote ? (
        <p className={cn("pause-mark", pauseNote && "pause-mark-above")} data-testid="paused-mark">paused — you sent a message</p>
      ) : null}
    </li>
  );
}, (prev, next) =>
  prev.last === next.last &&
  prev.sending === next.sending &&
  prev.streaming === next.streaming &&
  prev.freeze === next.freeze &&
  prev.pauseNote === next.pauseNote &&
  prev.botName === next.botName &&
  sameMessage(prev.message, next.message)
);

function frozenHasBody(frozen?: FrozenReply): boolean {
  return Boolean((frozen?.text || "").trim() || (frozen?.reasoning || "").trim());
}

function rowFrozen(message: ChatMessage, frozen?: FrozenReply): boolean {
  if (!frozen?.replyId || message.role !== "assistant") return false;
  return message.id === frozen.replyId || message.client_id === `live-${frozen.replyId}`;
}

const Transcript = memo(function Transcript({
  rows,
  botName,
  sending,
  liveKey,
  frozen,
  onChoose,
}: {
  rows: ChatMessage[];
  botName: string;
  sending: boolean;
  liveKey: string;
  frozen?: FrozenReply;
  onChoose: (choice: string) => void;
}) {
  return (
    <ol className="flex flex-col gap-4" data-testid="transcript">
      {rows.map((message, index) => {
        const live = Boolean(liveKey) && message.client_id === liveKey;
        const freeze = rowFrozen(message, frozen) || Boolean(message.interrupted);
        const pauseOnUser = Boolean(frozen && !frozenHasBody(frozen) && message.role === "user" && !rows.slice(index + 1).some((row) => row.role === "user"));
        return (
          <MessageRow
            key={rowKey(message, index)}
            message={message}
            last={index === rows.length - 1}
            botName={botName}
            sending={sending}
            streaming={live}
            freeze={freeze}
            pauseNote={pauseOnUser}
            onChoose={onChoose}
          />
        );
      })}
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
  const [error, setError] = useState("");
  const [askId, setAskId] = useState("");
  const [menu, setMenu] = useState(false);
  const { scrollRef, contentRef, isAtBottom, scrollToBottom } = useStickToBottom({ initial: "instant", resize: "instant" });
  const sectionRef = useRef<HTMLElement>(null);
  const dockRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<ComposerHandle>(null);
  const heldSteps = useRef<ActivityStep[]>([]);
  const seenRun = useRef("");
  const pinnedBusy = useRef(new Set<string>());
  const historyRef = useRef<ChatMessage[]>([]);
  const getHistory = useCallback(() => deriveUserHistory(historyRef.current), []);
  const bot = (bots.data || []).find((item) => item.id === botId) || null;
  const yoloNow = Date.now();
  const yoloLeft = yoloRemaining(chat.data?.yolo, chat.dataUpdatedAt || yoloNow, yoloNow);
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
  const serverRun = chat.data?.run?.status;
  const serverRunId = chat.data?.run?.id || "";
  useEffect(() => {
    if (!botId || !chatId || serverRun !== "running") return;
    const current = flightFor(botId, chatId);
    if (current && !current.replaced && current.sending && !current.settled) return;
    void attachLive(botId, chatId);
  }, [botId, chatId, serverRun, serverRunId]);
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

  const anchor = useRef<{ height: number; top: number } | null>(null);
  const loadingOlder = useRef(false);
  const messageCount = chat.data?.messages.length || 0;

  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    if (el.scrollTop < 64) void loadOlder();
  }

  async function loadOlder() {
    if (loadingOlder.current || !botId || !chatId) return;
    const start = chat.data?.window_start ?? 0;
    if (start <= 0) return;
    loadingOlder.current = true;
    try {
      const page = await api<MessagePage>(`/api/bots/${botId}/chats/${chatId}/messages?before=${start}&limit=80`);
      if (!page.messages.length || page.start >= start) return;
      const el = scrollRef.current;
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
    const el = scrollRef.current;
    if (!el) return;
    if (anchor.current) {
      el.scrollTop = anchor.current.top + (el.scrollHeight - anchor.current.height);
      anchor.current = null;
      return;
    }
    if (isAtBottom) el.scrollTop = el.scrollHeight;
  }, [messageCount, live?.text, isAtBottom, scrollRef]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !chat.data) return;
    if ((chat.data.window_start ?? 0) > 0 && el.scrollHeight <= el.clientHeight + 8) void loadOlder();
  }, [chat.data?.window_start, messageCount]);

  useEffect(() => {
    setMenu(false);
  }, [botId]);

  useEffect(() => {
    const dock = dockRef.current;
    const section = sectionRef.current;
    if (!dock || !section) return;
    let last = -1;
    const write = () => {
      const bucket = Math.max(8, Math.round(dock.getBoundingClientRect().height / 8) * 8);
      if (bucket === last) return;
      last = bucket;
      section.style.setProperty("--dock-h", `${bucket}px`);
    };
    write();
    const observer = new ResizeObserver(write);
    observer.observe(dock);
    return () => observer.disconnect();
  }, [bot?.id]);

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
  const named = shownStep(live?.label, open?.run?.current_step);
  const step = named || "Thinking";
  const now = Date.now();
  const view = open ? describeRun(step, open.run?.status || (flight?.sending ? "running" : "idle"), live?.run?.started_at || open.run?.started_at, live?.run?.last_activity_at || open.run?.last_activity_at, live?.heardAt, now) : null;
  const tone = view ? runTone(view.words, running ? "running" : open?.run?.status || "idle") : "thinking";
  const face = faceFor(Boolean(flight?.sending), live?.phase, view?.words, live?.text, running ? "running" : open?.run?.status, view?.words);
  const asking = (open?.messages || []).slice(-1)[0]?.choices;
  const rows = open?.messages || [];
  historyRef.current = rows;
  const runId = live?.run?.id || flight?.runId || "";
  if (runId && runId !== seenRun.current) {
    if (seenRun.current) heldSteps.current = [];
    seenRun.current = runId;
  }
  if (live?.steps?.length) heldSteps.current = live.steps;
  const steps = live?.steps || [];
  const liveTurn = Boolean(live && live.phase !== "error" && live.phase !== "stopped");
  const replyId = live?.replyId || live?.run?.reply_id || "";
  const stamp = replyId ? `live-${replyId}` : runId ? `live-${runId}` : "";
  const frozen = flight?.frozen;
  const streamingReply = Boolean(liveTurn && ((live?.text || "").trim() || (live?.reasoning || "").trim()));
  const placed = arrangeBusy(rows, pinnedBusy.current);
  let head = placed.head;
  const showFrozen = Boolean(frozen && frozenHasBody(frozen));
  if ((frozen ? showFrozen : streamingReply) && live && stamp) {
    head = placeLiveReply(
      head,
      {
        role: "assistant",
        content: frozen ? frozen.text : live.text,
        thinking: frozen ? frozen.reasoning : live.reasoning,
        thought_seconds: live.thoughtSeconds,
        client_id: stamp,
      },
      stamp,
      frozen?.replyId || replyId,
    );
  }
  if (frozen?.replyId) {
    head = head.map((row) => {
      if (row.role !== "assistant") return row;
      if (row.id !== frozen.replyId && row.client_id !== `live-${frozen.replyId}`) return row;
      if (row.content === frozen.text && (row.thinking || "") === frozen.reasoning) return row;
      return { ...row, content: frozen.text, thinking: frozen.reasoning };
    });
  }
  const shown = placed.tail.length ? [...head, ...placed.tail] : head;
  const asked = askedLine(bot.endpoint_name || "", rows);
  const pill = workingLine(live?.run || open?.run, now);
  const checklist = (open?.plan?.steps || []).filter((step) => step && step.text);
  const active = Boolean(open && (running || (flight?.sending && live && live.phase !== "error" && live.phase !== "stopped")));
  const presenceStep = named;
  const runStarted = live?.run?.started_at || open?.run?.started_at || live?.run?.activity_started_at || open?.run?.activity_started_at;
  const status = (
    <Elapsed>
      {(clock) => {
        const line = runPresence({ active, step: presenceStep, startedAt: runStarted, now: clock });
        return <span data-testid="run-state" data-state={line.kind}>{line.text}</span>;
      }}
    </Elapsed>
  );

  chooseRef.current = (choice) => {
    void submit(choice).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : "The reply failed.");
    });
    composerRef.current?.focus();
  };

  async function submit(text: string, file: File | null = null) {
    if (!botId || !chatId) return;
    if (!text && !file) return;
    setError("");
    void scrollToBottom("instant");
    if (typeof Notification !== "undefined" && Notification.permission === "default") void Notification.requestPermission();
    await sendMessage(botId, chatId, text, file);
  }

  return (
    <section ref={sectionRef} className="relative flex h-full min-h-0 flex-1 flex-col">
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
                  const task = composerRef.current?.text().trim() || "";
                  if (!askId || !task || !botId || !chatId) return;
                  composerRef.current?.clear();
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
            {yoloOn ? (
              <Elapsed>{(clock) => {
                const left = yoloRemaining(chat.data?.yolo, chat.dataUpdatedAt || clock, clock);
                return <>YOLO {yoloClock(left)}</>;
              }}</Elapsed>
            ) : "YOLO"}
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
          <span className="font-normal">
            <Elapsed>{(clock) => {
              const left = yoloRemaining(chat.data?.yolo, chat.dataUpdatedAt || clock, clock);
              return <>{yoloClock(left)} left in this chat</>;
            }}</Elapsed>
          </span>
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

      <div ref={scrollRef} onScroll={onScroll} className="chat-scroll min-h-0 flex-1 overflow-y-auto px-4 pt-2" style={{ overflowAnchor: "none" }}>
        <div ref={contentRef} className="mx-auto flex w-full max-w-[760px] flex-col gap-4">
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
                rows={shown}
                botName={bot?.name || "the bot"}
                sending={Boolean(flight?.sending)}
                liveKey={streamingReply ? stamp : ""}
                frozen={frozen}
                onChoose={onChoose}
              />
              {liveTurn && steps.length ? steps.map((item) => (
                <div key={item.id} data-testid="activity-step" data-state={item.state} data-step-id={item.id}>
                  <Quiet
                    icon={runTone(item.text, "running") === "tool" ? <Terminal className="h-3 w-3" /> : <MessageSquare className="h-3 w-3" />}
                    text={activityLabel(item)}
                  />
                </div>
              )) : heldSteps.current.length ? (
                <div data-testid="activity-summary">
                  <Quiet text={`Used ${heldSteps.current.length} steps`} detail={heldSteps.current.map((item) => activityLabel(item)).join("\n")} />
                </div>
              ) : null}
              {(open.messages || []).length || running ? (
                <div className="sys-line" role="status" data-testid="chat-working-pill" aria-hidden={running && view ? undefined : true}>
                  {running && view ? (
                    <p>
                      <span className={cn("h-1.5 w-1.5 rounded-full", tone === "halted" ? "bg-danger" : "bg-current")} />
                      <Elapsed>
                        {(clock) => (
                          <span data-testid="activity-line">
                            {activityLine({
                              step: named,
                              jobStartedAt: runStarted,
                              activityStartedAt: runStarted,
                              now: clock,
                              tokens: Math.floor((live?.chars || 0) / 4),
                            }) || workingLine(live?.run || open?.run, clock).text || view?.words}
                          </span>
                        )}
                      </Elapsed>
                      {pill.stuck ? <span>No activity for a while</span> : null}
                      {pill.queued ? <span data-testid="queued-badge">Queued</span> : null}
                    </p>
                  ) : (
                    <p className="sys-line-hold">Waiting on model, 0s</p>
                  )}
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

      {open && !isAtBottom ? (
        <button type="button" className="jump-latest" data-testid="jump-latest" onClick={() => void scrollToBottom("instant")}>
          Jump to latest
        </button>
      ) : null}
      <div ref={dockRef} className="composer-dock">
        <div className="px-4">
          {botId && bot ? <ApprovalCard botId={botId} botName={bot.name} active={running} /> : null}
        </div>
      <Composer
        ref={composerRef}
        focusKey={`${botId || ""}:${chatId || ""}`}
        running={running}
        asking={Boolean(asking && asking.length > 1)}
        error={error || (live?.phase === "error" ? live.text : "")}
        presence={status}
        resumeRetry={resume.retry}
        resumeCont={resume.cont}
        onSend={submit}
        onStop={() => {
          if (botId && chatId) void stopMessage(botId, chatId);
        }}
        onStopAndSend={async (text, file) => {
          if (!botId || !chatId) return;
          await stopMessage(botId, chatId);
          await sendMessage(botId, chatId, text, file);
        }}
        onRetry={() => {
          if (botId && chatId) void retryMessage(botId, chatId);
        }}
        onContinue={() => void submit("Continue")}
        onError={setError}
        botId={botId || ""}
        chatId={chatId || ""}
        menuOpen={menu}
        getHistory={getHistory}
      />
      </div>
    </section>
  );
}
