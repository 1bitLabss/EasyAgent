import { create } from "zustand";

export type Screen =
  | "chat"
  | "settings"
  | "connections"
  | "rooms"
  | "projects"
  | "computers"
  | "direction"
  | "about"
  | "search"
  | "room";

export type ConfirmRequest = {
  title: string;
  copy: string;
  name?: string;
  submit: string;
  run: () => Promise<void>;
};

/** Curated accent presets. "default" means no class on <html> and keeps the
 * monochrome look the app shipped with. The colors themselves live in
 * index.css as html.accent-<id> rules; the swatch is only a preview. */
export const ACCENTS = [
  { id: "default", label: "Default", swatch: "#171717" },
  { id: "blue", label: "Blue", swatch: "hsl(221 83% 45%)" },
  { id: "green", label: "Green", swatch: "hsl(142 66% 32%)" },
  { id: "violet", label: "Violet", swatch: "hsl(263 70% 45%)" },
  { id: "amber", label: "Amber", swatch: "hsl(32 90% 38%)" },
  { id: "rose", label: "Rose", swatch: "hsl(347 77% 44%)" },
] as const;

export type AccentId = (typeof ACCENTS)[number]["id"];
export type TextSize = "normal" | "large";

type AppState = {
  screen: Screen;
  botId: string | null;
  chatId: string | null;
  roomId: string | null;
  projectId: string | null;
  infoOpen: boolean;
  railOpen: boolean;
  theme: "light" | "dark";
  accent: AccentId;
  textSize: TextSize;
  switcher: boolean;
  offline: string;
  tokenNeeded: boolean;
  tick: number;
  focus: string;
  jumpTo: string;
  adding: boolean;
  nudge: string;
  confirm: ConfirmRequest | null;
  setScreen: (screen: Screen) => void;
  setAdding: (adding: boolean) => void;
  setNudge: (nudge: string) => void;
  goHome: () => void;
  selectBot: (botId: string) => void;
  openBot: (botId: string, chatId: string | null) => void;
  selectChat: (chatId: string | null) => void;
  setRoom: (roomId: string | null) => void;
  openRoom: (roomId: string) => void;
  jobsOpen: boolean;
  setJobs: (open: boolean) => void;
  highlightJob: string;
  setProject: (projectId: string | null) => void;
  setInfo: (open: boolean) => void;
  setRail: (open: boolean) => void;
  toggleTheme: () => void;
  setAccent: (accent: AccentId) => void;
  setTextSize: (size: TextSize) => void;
  setSwitcher: (open: boolean) => void;
  setOffline: (message: string) => void;
  setTokenNeeded: (needed: boolean) => void;
  bump: () => void;
  setFocus: (focus: string) => void;
  setJumpTo: (messageId: string) => void;
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

function savedAccent(): AccentId {
  try {
    const stored = localStorage.getItem("easyagent.accent");
    return ACCENTS.some((accent) => accent.id === stored) ? (stored as AccentId) : "default";
  } catch {
    return "default";
  }
}

function savedTextSize(): TextSize {
  try {
    return localStorage.getItem("easyagent.textSize") === "large" ? "large" : "normal";
  } catch {
    return "normal";
  }
}

/** The classes ride <html> so every CSS rule can see them. Only one accent
 * class is ever present; removing it restores the default look exactly. */
function applyAccent(accent: AccentId) {
  try {
    for (const item of ACCENTS) {
      if (item.id === "default") continue;
      document.documentElement.classList.toggle(`accent-${item.id}`, item.id === accent);
    }
  } catch {
    /* no document in this environment; the choice still persists */
  }
}

function applyTextSize(size: TextSize) {
  try {
    document.documentElement.classList.toggle("text-large", size === "large");
  } catch {
    /* same */
  }
}

function applyTheme(theme: "light" | "dark") {
  try {
    document.documentElement.classList.toggle("dark", theme === "dark");
  } catch {
    /* same */
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
  accent: savedAccent(),
  textSize: savedTextSize(),
  switcher: false,
  offline: "",
  tokenNeeded: false,
  tick: 0,
  focus: "",
  jumpTo: "",
  adding: false,
  nudge: "",
  confirm: null,
  setScreen: (screen) => set({ screen, railOpen: false, adding: false }),
  setAdding: (adding) => set(adding ? { adding: true, screen: "chat" } : { adding: false }),
  setNudge: (nudge) => set({ nudge }),
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
  openRoom: (roomId) => set({ roomId, screen: "room", railOpen: false }),
  jobsOpen: false,
  highlightJob: "",
  setJobs: (jobsOpen) => set({ jobsOpen }),
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
      applyTheme(theme);
      return { theme };
    }),
  setAccent: (accent) => {
    try {
      localStorage.setItem("easyagent.accent", accent);
    } catch {
      /* the choice still applies for this visit */
    }
    applyAccent(accent);
    set({ accent });
  },
  setTextSize: (textSize) => {
    try {
      localStorage.setItem("easyagent.textSize", textSize);
    } catch {
      /* the choice still applies for this visit */
    }
    applyTextSize(textSize);
    set({ textSize });
  },
  setSwitcher: (switcher) => set({ switcher }),
  setOffline: (offline) => set({ offline }),
  setTokenNeeded: (tokenNeeded) => set({ tokenNeeded }),
  bump: () => set((state) => ({ tick: state.tick + 1 })),
  setFocus: (focus) => set({ focus, screen: "settings" }),
  setJumpTo: (jumpTo) => set({ jumpTo }),
  askConfirm: (confirm) => set({ confirm }),
  closeConfirm: () => set({ confirm: null }),
}));

// A reload restores the saved look before the first paint, not after a mount.
applyTheme(useApp.getState().theme);
applyAccent(useApp.getState().accent);
applyTextSize(useApp.getState().textSize);
