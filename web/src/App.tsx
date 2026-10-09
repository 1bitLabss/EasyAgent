import { useQuery } from "@tanstack/react-query";
import { useEffect, useLayoutEffect } from "react";
import { api } from "@/api";
import { ChatPane } from "@/components/ChatPane";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Sheet } from "@/components/Sheet";
import { Sidebar } from "@/components/Sidebar";
import { Switcher } from "@/components/Switcher";
import { desktopOs } from "@/lib/desktop";
import { pickOpenBot } from "@/lib/open";
import { unreadRefetchInterval } from "@/lib/window";
import { installVisualViewport } from "@/lib/viewport";
import {
  AboutScreen,
  ComputersScreen,
  ConnectionsScreen,
  DirectionScreen,
  ProjectsScreen,
  RoomsScreen,
  TokenGate,
} from "@/screens/Places";
import { SettingsScreen } from "@/screens/Settings";
import { savedBotId, useApp } from "@/store";
import type { Bot } from "@/types";

export function App() {
  const theme = useApp((state) => state.theme);
  const screen = useApp((state) => state.screen);
  const setScreen = useApp((state) => state.setScreen);
  const setSwitcher = useApp((state) => state.setSwitcher);
  const offline = useApp((state) => state.offline);
  const botId = useApp((state) => state.botId);
  const selectBot = useApp((state) => state.selectBot);
  const bots = useQuery({ queryKey: ["bots"], queryFn: () => api<Bot[]>("/api/bots") });
  const unread = useQuery({
    queryKey: ["unread"],
    queryFn: () => api("/api/unread"),
    refetchInterval: unreadRefetchInterval,
    notifyOnChangeProps: [],
  });
  void unread;

  useLayoutEffect(() => {
    const ids = (bots.data || []).map((bot) => bot.id);
    const pick = pickOpenBot(ids, botId || savedBotId());
    if (!pick || pick === botId) return;
    selectBot(pick);
  }, [bots.data, botId, selectBot]);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

  useEffect(() => {
    const os = desktopOs();
    if (os) document.documentElement.dataset.desktop = os;
  }, []);

  useLayoutEffect(() => installVisualViewport(), []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const meta = event.metaKey || event.ctrlKey;
      if (!meta) return;
      const key = event.key.toLowerCase();
      if (key === "k") {
        event.preventDefault();
        setSwitcher(true);
      }
      if (key === "n") {
        event.preventDefault();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setSwitcher]);

  const main = screen === "settings" ? <SettingsScreen />
    : screen === "connections" ? <ConnectionsScreen />
    : screen === "rooms" ? <RoomsScreen />
    : screen === "projects" ? <ProjectsScreen />
    : screen === "computers" ? <ComputersScreen />
    : screen === "direction" ? <DirectionScreen />
    : screen === "about" ? <AboutScreen />
    : <ChatPane />;

  const sheetTitle = screen === "settings" ? "Settings"
    : screen === "connections" ? "Connections"
    : screen === "rooms" ? "Rooms"
    : screen === "projects" ? "Projects"
    : screen === "computers" ? "Computers"
    : screen === "direction" ? "Direction"
    : screen === "about" ? "About"
    : "";

  return (
    <div className="app-frame flex h-full flex-col bg-background text-foreground">
      {offline ? <p className="bg-danger px-3 py-1 text-sm text-white">{offline}</p> : null}
      {bots.isError ? <p className="px-3 py-2 text-sm text-danger" role="alert">{(bots.error as Error).message}</p> : null}
      <div className="app-body flex min-h-0 flex-1">
        <aside className="app-rail w-20 shrink-0"><Sidebar /></aside>
        <main className="relative flex min-h-0 min-w-0 flex-1 flex-col">
          {bots.isLoading ? <p className="p-6 text-sm text-muted">Loading bots…</p> : <ChatPane />}
          {screen !== "chat" ? <Sheet title={sheetTitle} onClose={() => setScreen("chat")}>{main}</Sheet> : null}
        </main>
      </div>
      <Switcher />
      <ConfirmDialog />
      <TokenGate />
    </div>
  );
}
