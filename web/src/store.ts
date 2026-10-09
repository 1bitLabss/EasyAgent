import { create } from "zustand";

export type Screen =
  | "chat"
  | "settings"
  | "connections"
  | "rooms"
  | "projects"
  | "computers"
  | "direction"
  | "about";

export type ConfirmRequest = {
  title: string;
  copy: string;
  name?: string;
  submit: string;
  run: () => Promise<void>;
};

type AppState = {
  screen: Screen;
  botId: string | null;
  chatId: string | null;
  roomId: string | null;
  projectId: string | null;
  infoOpen: boolean;
  railOpen: boolean;
  theme: "light" | "dark";
  switcher: boolean;
  offline: string;
  tokenNeeded: boolean;
  tick: number;
  focus: string;
  adding: boolean;
  confirm: ConfirmRequest | null;
  setScreen: (screen: Screen) => void;
  setAdding: (adding: boolean) => void;
  goHome: () => void;
  selectBot: (botId: string) => void;
  openBot: (botId: string, chatId: string | null) => void;
  selectChat: (chatId: string | null) => void;
  setRoom: (roomId: string | null) => void;
  setProject: (projectId: string | null) => void;
  setInfo: (open: boolean) => void;
  setRail: (open: boolean) => void;
  toggleTheme: () => void;
  setSwitcher: (open: boolean) => void;
  setOffline: (message: string) => void;
  setTokenNeeded: (needed: boolean) => void;
  bump: () => void;
  setFocus: (focus: string) => void;
  askConfirm: (confirm: ConfirmRequest) => void;
  closeConfirm: () => void;
};

function savedTheme(): "light" | "dark" {
  try {
    return localStorage.getItem("easyagent.theme") === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

export function savedBotId(): string | null {
  try {
    return localStorage.getItem("easyagent.bot");
  } catch {
    return null;
  }
}

function rememberBot(botId: string) {
  try {
    localStorage.setItem("easyagent.bot", botId);
  } catch {
    /* the next visit falls back to the first bot */
  }
}

export const useApp = create<AppState>((set) => ({
  screen: "chat",
  botId: null,
  chatId: null,
  roomId: null,
  projectId: null,
  infoOpen: true,
  railOpen: false,
  theme: savedTheme(),
  switcher: false,
  offline: "",
  tokenNeeded: false,
  tick: 0,
  focus: "",
  adding: false,
  confirm: null,
  setScreen: (screen) => set({ screen, railOpen: false, adding: false }),
  setAdding: (adding) => set(adding ? { adding: true, screen: "chat" } : { adding: false }),
  goHome: () => set({ screen: "chat", railOpen: false, adding: false }),
  selectBot: (botId) => {
    rememberBot(botId);
    set({ botId, chatId: null, screen: "chat", railOpen: false });
  },
  openBot: (botId, chatId) => {
    rememberBot(botId);
    set({ botId, chatId, screen: "chat", railOpen: false });
  },
  selectChat: (chatId) => set({ chatId, screen: "chat", railOpen: false }),
  setRoom: (roomId) => set({ roomId }),
  setProject: (projectId) => set({ projectId }),
  setInfo: (infoOpen) => set({ infoOpen }),
  setRail: (railOpen) => set({ railOpen }),
  toggleTheme: () =>
    set((state) => {
      const theme = state.theme === "dark" ? "light" : "dark";
      try {
        localStorage.setItem("easyagent.theme", theme);
      } catch {
        /* the choice still applies for this visit */
      }
      return { theme };
    }),
  setSwitcher: (switcher) => set({ switcher }),
  setOffline: (offline) => set({ offline }),
  setTokenNeeded: (tokenNeeded) => set({ tokenNeeded }),
  bump: () => set((state) => ({ tick: state.tick + 1 })),
  setFocus: (focus) => set({ focus, screen: "settings" }),
  askConfirm: (confirm) => set({ confirm }),
  closeConfirm: () => set({ confirm: null }),
}));
