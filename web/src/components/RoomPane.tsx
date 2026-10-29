import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/api";
import { ApprovalCard } from "@/components/ApprovalCard";
import { useApp } from "@/store";
import type { ChatMessage, Room } from "@/types";

type Member = { id: string; name: string; missing?: boolean; face_color?: string };
type RoomView = Room & { members?: Member[]; pending_approvals?: { id: string; bot_id: string; why?: string; detail?: string }[] };
type Status = { bot_id: string; name: string; state: string; label: string };

export function RoomPane() {
  const roomId = useApp((state) => state.roomId);
  const setScreen = useApp((state) => state.setScreen);
  const queryClient = useQueryClient();
  const room = useQuery({
    queryKey: ["room", roomId],
    enabled: Boolean(roomId),
    queryFn: () => api<RoomView>(`/api/rooms/${roomId}`),
  });
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState("");
  const [statuses, setStatuses] = useState<Status[]>([]);
  const [approvals, setApprovals] = useState<{ bot_id: string; name: string }[]>([]);
  const [error, setError] = useState("");
  const data = room.data;

  function rememberUser(message: ChatMessage) {
    if (!roomId) return;
    let placed = false;
    queryClient.setQueryData(["room", roomId], (current: RoomView | undefined) => {
      if (!current) return current;
      placed = true;
      const messages = current.messages || [];
      if (message.id && messages.some((item) => item.id === message.id)) return current;
      return { ...current, messages: [...messages, message] };
    });
    if (placed) setPending("");
  }

  function rememberApproval(botId: string, name: string) {
    if (!botId) return;
    setApprovals((current) => (current.some((item) => item.bot_id === botId) ? current : [...current, { bot_id: botId, name }]));
    void queryClient.invalidateQueries({ queryKey: ["approvals", botId] });
  }

  useEffect(() => {
    if (!roomId) return;
    const control = new AbortController();
    void (async () => {
      try {
        const response = await fetch(`/api/rooms/${roomId}/live`, { headers: { Accept: "text/event-stream" }, signal: control.signal });
        if (!response.ok || !response.body) return;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let split = buffer.indexOf("\n\n");
          while (split >= 0) {
            const raw = buffer.slice(0, split);
            buffer = buffer.slice(split + 2);
            const payload = raw.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("\n");
            if (payload) {
              const event = JSON.parse(payload) as { type?: string; room?: RoomView; bot_id?: string; name?: string; state?: string; label?: string; message?: ChatMessage };
              if (event.type === "user" && event.message) rememberUser(event.message);
              if (event.type === "approval" && event.bot_id) rememberApproval(event.bot_id, event.name || "");
              if (event.type === "bot_status" && event.bot_id) {
                setStatuses((current) => {
                  const next = current.filter((item) => item.bot_id !== event.bot_id);
                  next.push({ bot_id: event.bot_id || "", name: event.name || "", state: event.state || "", label: event.label || "" });
                  return next;
                });
              }
              if (event.room) queryClient.setQueryData(["room", roomId], event.room);
              if (event.type === "done") setPending("");
            }
            split = buffer.indexOf("\n\n");
          }
        }
      } catch {
        /* a reload attaches again */
      }
    })();
    return () => control.abort();
  }, [roomId, queryClient]);

  async function send() {
    const text = draft.trim();
    if (!roomId || !text) return;
    setDraft("");
    setPending(text);
    setError("");
    setStatuses([]);
    try {
      const response = await fetch(`/api/rooms/${roomId}/messages`, {
        method: "POST",
        headers: { Accept: "text/event-stream", "Content-Type": "application/json" },
        body: JSON.stringify({ content: text }),
      });
      if (!response.ok || !response.body) {
        setError("The room did not take that message.");
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let split = buffer.indexOf("\n\n");
        while (split >= 0) {
          const raw = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          const payload = raw.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("\n");
          if (!payload) {
            split = buffer.indexOf("\n\n");
            continue;
          }
          const event = JSON.parse(payload) as { type?: string; room?: RoomView; message?: ChatMessage; bot_id?: string; name?: string; state?: string; label?: string };
          if (event.type === "user" && event.message) rememberUser(event.message);
          else if (event.type === "user") setPending("");
          if (event.type === "approval" && event.bot_id) rememberApproval(event.bot_id, event.name || "");
          if (event.type === "bot_status" && event.bot_id) {
            setStatuses((current) => {
              const next = current.filter((item) => item.bot_id !== event.bot_id);
              next.push({ bot_id: event.bot_id || "", name: event.name || "", state: event.state || "", label: event.label || "" });
              return next;
            });
          }
          if (event.type === "message" && event.message) {
            queryClient.setQueryData(["room", roomId], (current: RoomView | undefined) => {
              if (!current) return current;
              return { ...current, messages: [...(current.messages || []), event.message as ChatMessage] };
            });
            setStatuses((current) => current.filter((item) => item.bot_id !== event.message?.speaker));
          }
          if (event.room) queryClient.setQueryData(["room", roomId], event.room);
          split = buffer.indexOf("\n\n");
        }
      }
      await queryClient.invalidateQueries({ queryKey: ["room", roomId] });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The room did not answer.");
    } finally {
      setPending("");
    }
  }

  if (!roomId) return <p className="p-6 text-sm text-muted">Pick a room.</p>;
  if (room.isLoading) return <p className="p-6 text-sm text-muted">Loading the room…</p>;
  if (room.isError || !data) return <p className="p-6 text-sm text-danger" role="alert">That room could not be opened.</p>;

  const messages = data.messages || [];
  return (
    <section className="flex h-full min-h-0 flex-1 flex-col" data-testid="room-window">
      <header className="flex items-center gap-2 border-b border-border px-4 py-3">
        <h1 className="text-base font-semibold">{data.name}</h1>
        <div className="flex flex-wrap gap-1">
          {(data.members || []).map((member) => {
            const live = statuses.find((item) => item.bot_id === member.id);
            return (
              <span key={member.id} data-testid="room-member-chip" data-state={live?.state || "idle"} className="rounded-full border border-border px-2 py-0.5 text-xs">
                {member.name}
              </span>
            );
          })}
        </div>
        <button type="button" className="ml-auto text-sm text-muted" onClick={() => setScreen("chat")}>Close</button>
      </header>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-4">
        {messages.length === 0 && !pending ? <p className="text-sm text-muted">This room is empty. Say something and each member answers in turn.</p> : null}
        {messages.map((message) => (
          <article key={message.id} className={message.role === "user" ? "ml-auto max-w-lg text-right" : "max-w-lg"}>
            {message.role !== "user" ? <p className="text-xs text-muted" data-testid="room-speaker">{message.speaker_name}</p> : <p className="text-xs text-muted">You</p>}
            <p className="whitespace-pre-wrap text-sm">{message.content}</p>
          </article>
        ))}
        {pending ? <article className="ml-auto max-w-lg text-right"><p className="text-xs text-muted">You</p><p className="text-sm" data-testid="room-optimistic">{pending}</p></article> : null}
        {statuses.length ? (
          <p className="text-sm text-muted" data-testid="room-bot-status">{statuses.map((item) => item.label).join(" · ")}</p>
        ) : null}
        {(data.pending_approvals || []).map((card) => (
          <div key={card.id} data-testid="room-approval">
            <p className="text-xs text-muted">{card.why}</p>
            <ApprovalCard botId={card.bot_id} botName="" active />
          </div>
        ))}
        {approvals.filter((card) => !(data.pending_approvals || []).some((item) => item.bot_id === card.bot_id)).map((card) => (
          <div key={card.bot_id} data-testid="room-approval">
            <ApprovalCard botId={card.bot_id} botName={card.name} active />
          </div>
        ))}
        {error ? <p className="text-sm text-danger" role="alert">{error}</p> : null}
      </div>
      <form className="border-t border-border p-3" onSubmit={(event) => { event.preventDefault(); void send(); }}>
        <textarea
          className="w-full rounded-lg border border-border bg-card p-2 text-sm"
          data-testid="room-draft"
          value={draft}
          placeholder="Message the room. Enter to send. Use @Name to ask one member."
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.nativeEvent.isComposing || event.key !== "Enter" || event.shiftKey) return;
            event.preventDefault();
            void send();
          }}
        />
        <div className="mt-2 flex gap-2">
          <button type="submit" className="rounded-full bg-foreground px-3 py-1 text-sm text-background">Send</button>
          <button type="button" className="text-sm text-muted" onClick={() => void api(`/api/rooms/${roomId}/stop`, { method: "POST" })}>Stop</button>
        </div>
      </form>
    </section>
  );
}
