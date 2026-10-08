import { useQuery } from "@tanstack/react-query";
import { PanelRightClose, PanelRightOpen } from "lucide-react";
import { api } from "@/api";
import { Face } from "@/components/Face";
import { Button } from "@/components/ui/button";
import { contextNote, describeRun } from "@/lib/run";
import { useApp } from "@/store";
import { flightFor } from "@/stream";
import type { Bot, Chat, Learning } from "@/types";

export function InfoPane() {
  const open = useApp((state) => state.infoOpen);
  const setInfo = useApp((state) => state.setInfo);
  const botId = useApp((state) => state.botId);
  const chatId = useApp((state) => state.chatId);
  const tick = useApp((state) => state.tick);
  const setFocus = useApp((state) => state.setFocus);
  const setScreen = useApp((state) => state.setScreen);
  const bots = useQuery({ queryKey: ["bots"], queryFn: () => api<Bot[]>("/api/bots") });
  const chat = useQuery({
    queryKey: ["chat", botId, chatId],
    enabled: Boolean(botId && chatId),
    queryFn: () => api<Chat>(`/api/bots/${botId}/chats/${chatId}?window=80`),
  });
  const learning = useQuery({
    queryKey: ["learning", botId],
    enabled: Boolean(botId),
    queryFn: () => api<Learning>(`/api/bots/${botId}/learning`),
  });
  const bot = (bots.data || []).find((item) => item.id === botId);
  void tick;
  const flight = botId && chatId ? flightFor(botId, chatId) : null;
  const run = flight?.live?.run || chat.data?.run;
  const described = run ? describeRun(flight?.live?.label || run.current_step || "Thinking", run.status, run.started_at, run.last_activity_at, flight?.live?.heardAt, Date.now()) : null;

  if (!open) {
    return (
      <div className="hidden w-11 shrink-0 justify-center border-l border-border py-3 lg:flex">
        <Button variant="ghost" size="icon" aria-label="Show details" onClick={() => setInfo(true)}>
          <PanelRightOpen className="h-4 w-4" />
        </Button>
      </div>
    );
  }

  return (
    <aside className="absolute inset-y-0 right-0 z-20 flex w-72 flex-col border-l border-border bg-card shadow-xl lg:static lg:w-80 lg:shadow-none">
      <div className="flex items-center justify-between px-3 py-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Details</h2>
        <Button variant="ghost" size="icon" aria-label="Hide details" onClick={() => setInfo(false)}>
          <PanelRightClose className="h-4 w-4" />
        </Button>
      </div>
      {!bot ? <p className="px-4 text-sm text-muted">Pick a bot to see its details.</p> : (
        <div className="space-y-4 overflow-y-auto px-4 pb-6">
          <div className="flex items-center gap-3">
            <Face color={bot.face_color} large />
            <div>
              <p className="font-semibold">{bot.name}</p>
              <p className="text-xs text-muted">{bot.endpoint_name || "No connection"}{bot.model ? ` · ${bot.model}` : ""}</p>
            </div>
          </div>
          {described && run?.status === "running" ? (
            <p className="text-sm" role="status">{described.words} {described.elapsed}</p>
          ) : null}
          <p className="text-xs text-muted">{contextNote(chat.data?.context, "chat")}</p>
          <p className="text-xs text-muted">{bot.check_enabled ? "Check replies before sending is on." : "Check replies before sending is off."} EasyAgent uses your connected model to review and learn — no extra model needed.</p>
          <p className="text-xs text-muted">
            {learning.data?.paused ? "Learning is paused while this bot is idle." : "Learning can run while this bot is idle."}
            {" "}
            {learning.data?.waiting?.length ? `${learning.data.waiting.length} waiting.` : "No candidates waiting."}
          </p>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" onClick={() => setScreen("settings")}>Settings</Button>
            <Button size="sm" variant="outline" onClick={() => setFocus("memory")}>Memory</Button>
            <Button size="sm" variant="outline" onClick={() => setFocus("learning")}>Learning</Button>
          </div>
        </div>
      )}
    </aside>
  );
}
