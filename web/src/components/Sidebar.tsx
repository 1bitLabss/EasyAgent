import { useQuery } from "@tanstack/react-query";
import { LayoutGrid, Moon, Plus, Sun, UserRound } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, queryClient } from "@/api";
import { Face, STATE_COLORS } from "@/components/Face";
import { Sheet } from "@/components/Sheet";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { faceStateFor } from "@/lib/run";
import { cn } from "@/lib/utils";
import { useApp } from "@/store";
import { anySending } from "@/stream";
import type { Bot, Chat, Endpoint, Unread } from "@/types";

const RING: Record<string, string> = {
  waiting: STATE_COLORS.waiting,
  reconnecting: STATE_COLORS.reconnecting,
  thinking: STATE_COLORS.thinking,
  tool: STATE_COLORS.tool,
  talking: "",
  halted: STATE_COLORS.halted,
};

function BotTile({ bot }: { bot: Bot }) {
  const selected = useApp((state) => state.botId) === bot.id;
  const chatId = useApp((state) => state.chatId);
  const tick = useApp((state) => state.tick);
  const selectBot = useApp((state) => state.selectBot);
  const unread = useQuery({
    queryKey: ["unread"],
    queryFn: () => api<Unread>("/api/unread"),
    enabled: false,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
  });
  const chat = useQuery({
    queryKey: ["chat", bot.id, chatId],
    enabled: selected && Boolean(chatId),
    queryFn: () => api<Chat>(`/api/bots/${bot.id}/chats/${chatId}?window=80`),
  });
  void tick;
  const flight = anySending(bot.id);
  const count = (unread.data?.chats || []).filter((row) => row.bot_id === bot.id).reduce((sum, row) => sum + row.unread, 0);
  const state = faceStateFor({
    sending: Boolean(flight?.sending),
    phase: flight?.live?.phase,
    label: flight?.live?.label || flight?.live?.run?.current_step,
    text: flight?.live?.text,
    runStatus: flight?.live?.run?.status || (selected ? chat.data?.run?.status : ""),
    step: flight?.live?.run?.current_step || (selected ? chat.data?.run?.current_step : ""),
    busy: Boolean(unread.data?.busy?.includes(bot.id)),
  });
  const live = state !== "idle";
  const ring = state === "talking" ? bot.face_color : RING[state];
  const [tip, setTip] = useState<{ top: number; left: number } | null>(null);
  function placeTip(target: HTMLElement) {
    const rect = target.getBoundingClientRect();
    setTip({ top: rect.top + rect.height / 2, left: rect.right + 10 });
  }
  return (
    <>
      <button
        type="button"
        aria-label={count > 0 ? `${bot.name}, ${count} unread` : bot.name}
        onClick={() => selectBot(bot.id)}
        onMouseEnter={(event) => placeTip(event.currentTarget)}
        onMouseLeave={() => setTip(null)}
        onFocus={(event) => placeTip(event.currentTarget)}
        onBlur={() => setTip(null)}
        className={cn("relative flex h-[52px] w-[52px] items-center justify-center rounded-2xl", selected && "bg-[#ececec] dark:bg-white/10")}
      >
        <span className={cn("face-ring rounded-[14px]", live && "is-live")} style={{ ["--ring" as string]: ring || bot.face_color }}>
          <Face color={bot.face_color} state={state} botId={bot.id} tile />
        </span>
        {count > 0 ? <span className="absolute right-1.5 top-1.5 h-2 w-2 rounded-full bg-foreground" /> : null}
      </button>
      {tip
        ? createPortal(
            <span className="rail-tip pixel" role="tooltip" style={{ top: tip.top, left: tip.left }}>
              {bot.name}
            </span>,
            document.body,
          )
        : null}
    </>
  );
}

function AddBotForm({ onClose }: { onClose: () => void }) {
  const endpoints = useQuery({ queryKey: ["endpoints"], queryFn: () => api<Endpoint[]>("/api/endpoints") });
  const openBot = useApp((state) => state.openBot);
  const [error, setError] = useState("");

  async function createBot(form: FormData) {
    setError("");
    const name = String(form.get("name") || "");
    let endpointId = String(form.get("endpoint_id") || "");
    if (endpointId === "new") {
      const created = await api<Endpoint>("/api/endpoints", {
        method: "POST",
        json: {
          name: String(form.get("conn_name") || ""),
          base_url: String(form.get("conn_url") || ""),
          api_key: String(form.get("conn_key") || "") || null,
          model: String(form.get("conn_model") || "") || null,
        },
      });
      endpointId = created.id;
      await queryClient.invalidateQueries({ queryKey: ["endpoints"] });
    }
    const bot = await api<Bot>("/api/bots", {
      method: "POST",
      json: {
        name,
        endpoint_id: endpointId,
        model: String(form.get("model") || "") || null,
        context_tokens: Number(form.get("context_tokens") || 24000),
      },
    });
    const chat = await api<Chat>(`/api/bots/${bot.id}/ongoing`);
    await queryClient.invalidateQueries({ queryKey: ["bots"] });
    await queryClient.invalidateQueries({ queryKey: ["chats", bot.id] });
    openBot(bot.id, chat.id);
    onClose();
  }

  return (
    <form
      className="space-y-3 px-5 pb-8"
      onSubmit={(event) => {
        event.preventDefault();
        void createBot(new FormData(event.currentTarget)).catch((reason: Error) => setError(reason.message));
      }}
    >
      <p className="text-sm text-muted">A bot is one chat partner. Its chats stay private.</p>
      <label className="block text-sm">Name
        <Input name="name" required maxLength={80} placeholder="Ada" className="mt-1" />
      </label>
      <label className="block text-sm">Connection
        <select name="endpoint_id" required className="mt-1 h-9 w-full rounded-md border border-border bg-card px-2 text-sm" defaultValue={endpoints.data?.[0]?.id || "new"}>
          {(endpoints.data || []).map((endpoint) => (
            <option key={endpoint.id} value={endpoint.id}>{endpoint.name}</option>
          ))}
          <option value="new">New connection</option>
        </select>
      </label>
      <label className="block text-sm">Connection name
        <Input name="conn_name" maxLength={80} placeholder="Home server" className="mt-1" />
      </label>
      <label className="block text-sm">Address
        <Input name="conn_url" placeholder="http://localhost:8080/v1" className="mt-1" />
      </label>
      <label className="block text-sm">Key <span className="text-muted">optional</span>
        <Input name="conn_key" type="password" autoComplete="off" className="mt-1" />
      </label>
      <label className="block text-sm">Model name <span className="text-muted">optional</span>
        <Input name="conn_model" maxLength={120} className="mt-1" />
      </label>
      <label className="block text-sm">Model name for this bot <span className="text-muted">optional</span>
        <Input name="model" maxLength={120} placeholder="only if it should differ" className="mt-1" />
      </label>
      <label className="block text-sm">How much chat it sees <span className="text-muted">tokens</span>
        <Input name="context_tokens" type="number" min={512} max={1000000} defaultValue={24000} className="mt-1" />
      </label>
      {error ? <p className="text-sm text-danger" role="alert">{error}</p> : null}
      <Button type="submit">Add bot</Button>
    </form>
  );
}

export function Sidebar() {
  const screen = useApp((state) => state.screen);
  const setScreen = useApp((state) => state.setScreen);
  const botId = useApp((state) => state.botId);
  const goHome = useApp((state) => state.goHome);
  const theme = useApp((state) => state.theme);
  const toggleTheme = useApp((state) => state.toggleTheme);
  const setFocus = useApp((state) => state.setFocus);
  const adding = useApp((state) => state.adding);
  const setAdding = useApp((state) => state.setAdding);
  const bots = useQuery({ queryKey: ["bots"], queryFn: () => api<Bot[]>("/api/bots") });
  const [menu, setMenu] = useState(false);
  const [templates, setTemplates] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menu) return;
    const close = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenu(false);
    };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [menu]);

  const places: { id: typeof screen; label: string }[] = [
    { id: "connections", label: "Connections" },
    { id: "rooms", label: "Rooms" },
    { id: "projects", label: "Projects" },
    { id: "computers", label: "Computers" },
    { id: "direction", label: "Direction" },
    { id: "about", label: "About" },
  ];

  return (
    <>
      <div className="rail flex h-full w-20 flex-col items-center bg-[#fafafa] py-3 dark:bg-[#111]">
        <button
          type="button"
          aria-label="Home"
          title="Home"
          onClick={goHome}
          className={cn("flex h-[52px] w-[52px] items-center justify-center rounded-2xl", !botId && screen === "chat" && "bg-[#ececec] dark:bg-white/10")}
        >
          <img src="/static/mascot.svg" alt="" width={36} height={38} />
        </button>
        <div className="rail-divider my-2 h-px w-8 bg-black/10 dark:bg-white/15" />
        <div className="rail-bots flex min-h-0 w-full flex-1 flex-col items-center gap-1 overflow-y-auto py-1">
          {(bots.data || []).map((bot) => <BotTile key={bot.id} bot={bot} />)}
        </div>
        <div className="rail-tools mt-2 flex flex-col items-center gap-1 pb-1">
          <button type="button" aria-label="Add bot" title="Add bot" className="flex h-10 w-10 items-center justify-center rounded-full text-foreground hover:bg-black/5 dark:hover:bg-white/10" onClick={() => setAdding(true)}>
            <Plus className="h-5 w-5" />
          </button>
          <button
            type="button"
            aria-label="Templates"
            title="Templates"
            className="flex h-10 w-10 items-center justify-center rounded-full text-muted hover:bg-black/5 dark:hover:bg-white/10"
            onClick={() => {
              if (botId) setFocus("skills");
              else setTemplates(true);
            }}
          >
            <LayoutGrid className="h-4 w-4" />
          </button>
          <div className="relative" ref={menuRef}>
            <button
              type="button"
              aria-label="You"
              title="You"
              className="flex h-9 w-9 items-center justify-center rounded-full bg-[#e6e1da] text-[#5c5348] dark:bg-[#2a2a2a] dark:text-[#d9d3cb]"
              onClick={() => setMenu((open) => !open)}
            >
              <UserRound className="h-4 w-4" />
            </button>
            {menu ? (
              <div className="rail-menu absolute bottom-0 left-12 z-40 w-48 rounded-2xl bg-background p-1.5 shadow-[0_8px_30px_rgba(0,0,0,0.12)]">
                {places.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className="block w-full rounded-xl px-3 py-2 text-left text-sm hover:bg-black/5 dark:hover:bg-white/10"
                    onClick={() => { setMenu(false); setScreen(item.id); }}
                  >
                    {item.label}
                  </button>
                ))}
                <button type="button" className="block w-full rounded-xl px-3 py-2 text-left text-sm hover:bg-black/5 dark:hover:bg-white/10" onClick={() => { setMenu(false); setFocus("phone"); }}>
                  Phone
                </button>
                {botId ? (
                  <button type="button" className="block w-full rounded-xl px-3 py-2 text-left text-sm hover:bg-black/5 dark:hover:bg-white/10" onClick={() => { setMenu(false); setScreen("settings"); }}>
                    This bot's settings
                  </button>
                ) : null}
                <button type="button" className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-sm hover:bg-black/5 dark:hover:bg-white/10" onClick={() => { toggleTheme(); setMenu(false); }}>
                  {theme === "dark" ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
                  {theme === "dark" ? "Light" : "Dark"}
                </button>
              </div>
            ) : null}
          </div>
        </div>
      </div>
      {adding ? (
        <Sheet title="New bot" onClose={() => setAdding(false)}>
          <AddBotForm onClose={() => setAdding(false)} />
        </Sheet>
      ) : null}
      {templates ? (
        <Sheet title="Templates" onClose={() => setTemplates(false)}>
          <p className="px-5 pb-8 text-sm text-muted">Skills live on a bot. Add a bot, then open Templates again.</p>
        </Sheet>
      ) : null}
    </>
  );
}
