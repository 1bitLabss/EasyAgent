import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { useApp } from "@/store";
import { resetStreamForTests } from "@/stream";
import { resetSpeakForTests, stopSpeaking } from "@/lib/speak";
import type { Bot, Chat, ChatMessage } from "@/types";
import { ChatPane } from "./ChatPane";

vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnWindowFocus: false } },
  });
  return {
    queryClient: client,
    api: vi.fn(async (path: string) => {
      if (path === "/api/bots") return [bot()];
      if (path.includes("/ongoing")) return { id: "chat-1" };
      if (path.includes("/approvals")) return [];
      if (path.includes("/read")) return {};
      return chat([]);
    }),
  };
});

function bot(): Bot {
  return {
    id: "bot-1",
    name: "Ada",
    endpoint_id: "cedar",
    endpoint_name: "Cedar",
    endpoint_base_url: null,
    model: null,
    context_tokens: 8192,
    face_color: "#6b8f71",
    face_color_set: true,
    check_enabled: false,
    learn_paused: true,
    learn_manual: true,
  };
}

function chat(messages: ChatMessage[]): Chat {
  return {
    id: "chat-1",
    bot_id: "bot-1",
    title: "Ada",
    messages,
    message_count: messages.length,
    window_start: 0,
    run: {
      id: "run-1",
      status: "idle",
      started_at: "2026-11-07T18:00:00Z",
      last_activity_at: "2026-11-07T18:00:53Z",
      current_step: "",
      reason: "",
      activity_started_at: "2026-11-07T18:00:00Z",
    },
  };
}

function paint(messages: ChatMessage[]) {
  queryClient.setQueryData(["bots"], [bot()]);
  queryClient.setQueryData(["ongoing", "bot-1"], { id: "chat-1" });
  queryClient.setQueryData(["chat", "bot-1", "chat-1"], chat(messages));
  queryClient.setQueryData(["approvals", "bot-1"], []);
  useApp.getState().openBot("bot-1", "chat-1");
  render(
    <QueryClientProvider client={queryClient}>
      <ChatPane />
    </QueryClientProvider>,
  );
}

const ask: ChatMessage = { id: "u1", role: "user", content: "how many boats", created_at: "2026-11-07T18:00:00Z" };

function answer(id: string, content: string, extra: Partial<ChatMessage> = {}): ChatMessage {
  return { id, role: "assistant", content, created_at: "2026-11-07T18:00:12Z", ...extra };
}

const speakSpy = vi.fn();
const cancelSpy = vi.fn();
const made: { text: string; onend?: (() => void) | null }[] = [];

class FakeUtterance {
  text: string;
  onend: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(text: string) {
    this.text = text;
    made.push(this);
  }
}

function installVoice() {
  Object.defineProperty(window, "speechSynthesis", {
    configurable: true,
    value: { speak: speakSpy, cancel: cancelSpy },
  });
  Object.defineProperty(window, "SpeechSynthesisUtterance", {
    configurable: true,
    value: FakeUtterance,
  });
}

function removeVoice() {
  delete (window as { speechSynthesis?: unknown }).speechSynthesis;
  delete (window as { SpeechSynthesisUtterance?: unknown }).SpeechSynthesisUtterance;
}

// The server mp3 path: a fetch that answers, an Audio that plays, and the
// object-URL pair the player needs.
const fetchSpy = vi.fn();
const playSpy = vi.fn();
const pauseSpy = vi.fn();
let audioEnded: (() => void) | null = null;

class FakeAudio {
  src = "";
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(url: string) {
    this.src = url;
  }
  play() {
    audioEnded = () => this.onended?.();
    playSpy();
    return Promise.resolve();
  }
  pause() {
    pauseSpy();
  }
}

function installServerVoice(status = 200) {
  fetchSpy.mockImplementation(async () =>
    status === 200
      ? { ok: true, status, blob: async () => new Blob(["mp3"], { type: "audio/mpeg" }) }
      : { ok: false, status, blob: async () => new Blob([]) },
  );
  Object.defineProperty(window, "fetch", { configurable: true, value: fetchSpy });
  Object.defineProperty(window, "Audio", { configurable: true, value: FakeAudio });
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:mp3") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
}

// This jsdom build ships no localStorage; the app guards it, the tests stub it.
const memoryStore = new Map<string, string>();
Object.defineProperty(window, "localStorage", {
  configurable: true,
  value: {
    getItem: (key: string) => (memoryStore.has(key) ? memoryStore.get(key) : null),
    setItem: (key: string, value: string) => void memoryStore.set(key, value),
    removeItem: (key: string) => void memoryStore.delete(key),
  },
});

beforeEach(() => {
  installVoice();
  // No server voice by default: /tts answers 404 and the browser voice takes over.
  installServerVoice(404);
});

afterEach(() => {
  cleanup();
  resetStreamForTests();
  queryClient.clear();
  useApp.setState({ botId: null, chatId: null, screen: "chat" });
  memoryStore.clear();
  stopSpeaking();
  resetSpeakForTests();
  removeVoice();
  speakSpy.mockClear();
  cancelSpy.mockClear();
  fetchSpy.mockReset();
  playSpy.mockClear();
  pauseSpy.mockClear();
  made.length = 0;
  audioEnded = null;
});

describe("the quiet speak control", () => {
  it("speaks the reply with markdown stripped", async () => {
    paint([ask, answer("a1", "**Twelve** boats.\n\n```python\nprint(1)\n```\n\nThat is all.")]);
    fireEvent.click(screen.getByTestId("speak-button"));
    await waitFor(() => expect(speakSpy).toHaveBeenCalledTimes(1));
    expect(made[0]?.text).toBe("Twelve boats. That is all.");
  });

  it("plays the server mp3 when the bot has a server voice", async () => {
    installServerVoice(200);
    paint([ask, answer("a1", "Twelve boats.")]);
    fireEvent.click(screen.getByTestId("speak-button"));
    await waitFor(() => expect(playSpy).toHaveBeenCalledTimes(1));
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/bots/bot-1/tts",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ text: "Twelve boats." }) }),
    );
    expect(speakSpy).not.toHaveBeenCalled();
    act(() => audioEnded?.());
    expect(screen.getByTestId("speak-button").textContent).toBe("Speak");
  });

  it("falls back to the browser voice when the server has no /tts", async () => {
    paint([ask, answer("a1", "Twelve boats.")]);
    fireEvent.click(screen.getByTestId("speak-button"));
    await waitFor(() => expect(speakSpy).toHaveBeenCalledTimes(1));
  });

  it("asks the server once after a 404, then goes straight to the browser", async () => {
    paint([ask, answer("a1", "Twelve boats."), answer("a2", "Nine ships.")]);
    const buttons = screen.getAllByTestId("speak-button");
    fireEvent.click(buttons[0]);
    await waitFor(() => expect(speakSpy).toHaveBeenCalledTimes(1));
    fireEvent.click(buttons[1]);
    await waitFor(() => expect(speakSpy).toHaveBeenCalledTimes(2));
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("clicking again stops instead of speaking twice", async () => {
    paint([ask, answer("a1", "Twelve boats.")]);
    const button = screen.getByTestId("speak-button");
    fireEvent.click(button);
    await waitFor(() => expect(speakSpy).toHaveBeenCalledTimes(1));
    cancelSpy.mockClear();
    expect(button.textContent).toBe("Stop");
    fireEvent.click(button);
    expect(speakSpy).toHaveBeenCalledTimes(1);
    expect(cancelSpy).toHaveBeenCalledTimes(1);
    expect(button.textContent).toBe("Speak");
  });

  it("starting another message cancels the first", async () => {
    paint([ask, answer("a1", "Twelve boats."), answer("a2", "Nine ships.")]);
    const buttons = screen.getAllByTestId("speak-button");
    fireEvent.click(buttons[0]);
    await waitFor(() => expect(speakSpy).toHaveBeenCalledTimes(1));
    cancelSpy.mockClear();
    fireEvent.click(buttons[1]);
    await waitFor(() => expect(speakSpy).toHaveBeenCalledTimes(2));
    expect(cancelSpy).toHaveBeenCalledTimes(1);
    expect(made.map((utterance) => utterance.text)).toEqual(["Twelve boats.", "Nine ships."]);
  });

  it("switching chats cancels mid-sentence", async () => {
    paint([ask, answer("a1", "Twelve boats.")]);
    fireEvent.click(screen.getByTestId("speak-button"));
    await waitFor(() => expect(speakSpy).toHaveBeenCalledTimes(1));
    cancelSpy.mockClear();
    act(() => {
      useApp.setState({ chatId: "chat-2" });
    });
    expect(cancelSpy).toHaveBeenCalled();
  });

  it("stays hidden when the webview has no speechSynthesis and the server has no voice", () => {
    removeVoice();
    paint([ask, answer("a1", "Twelve boats.")]);
    expect(screen.queryByTestId("speak-button")).toBeNull();
  });

  it("shows Speak without speechSynthesis when the bot has a server voice", async () => {
    removeVoice();
    installServerVoice(200);
    paint([ask, answer("a1", "Twelve boats.")]);
    await waitFor(() => expect(screen.getByTestId("speak-button")).toBeTruthy());
    fireEvent.click(screen.getByTestId("speak-button"));
    await waitFor(() => expect(playSpy).toHaveBeenCalledTimes(1));
    expect(speakSpy).not.toHaveBeenCalled();
    // The voices probe ran once; the mp3 came from the server, never speechSynthesis.
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/bots/bot-1/tts/voices",
      expect.anything(),
    );
    expect(
      fetchSpy.mock.calls.filter((call) => String(call[0]).includes("/tts/voices")),
    ).toHaveLength(1);
    act(() => audioEnded?.());
    expect(screen.getByTestId("speak-button").textContent).toBe("Speak");
  });

  it("Stop on the server-only path cancels the audio without touching speechSynthesis", async () => {
    removeVoice();
    installServerVoice(200);
    paint([ask, answer("a1", "Twelve boats.")]);
    await waitFor(() => expect(screen.getByTestId("speak-button")).toBeTruthy());
    fireEvent.click(screen.getByTestId("speak-button"));
    await waitFor(() => expect(playSpy).toHaveBeenCalledTimes(1));
    const button = screen.getByTestId("speak-button");
    expect(button.textContent).toBe("Stop");
    fireEvent.click(button);
    expect(pauseSpy).toHaveBeenCalledTimes(1);
    expect(cancelSpy).not.toHaveBeenCalled();
    expect(button.textContent).toBe("Speak");
  });

  it("never shows on a row that is still streaming", () => {
    paint([ask, answer("a1", "Twelve boats.", { live: true })]);
    expect(screen.queryByTestId("speak-button")).toBeNull();
  });
});

describe("read replies aloud", () => {
  it("auto-speaks a new finished reply in the open chat", async () => {
    localStorage.setItem("easyagent.readAloud", "on");
    paint([ask, answer("a1", "Twelve boats.")]);
    expect(speakSpy).not.toHaveBeenCalled();
    act(() => {
      queryClient.setQueryData(
        ["chat", "bot-1", "chat-1"],
        chat([ask, answer("a1", "Twelve boats."), answer("a2", "**Nine** ships.")]),
      );
    });
    await waitFor(() => expect(speakSpy).toHaveBeenCalledTimes(1));
    expect(made[0]?.text).toBe("Nine ships.");
  });

  it("does not speak the history that was already there", () => {
    localStorage.setItem("easyagent.readAloud", "on");
    paint([ask, answer("a1", "Twelve boats."), answer("a2", "Nine ships.")]);
    expect(speakSpy).not.toHaveBeenCalled();
  });

  it("waits for a live row to finish before speaking it", async () => {
    localStorage.setItem("easyagent.readAloud", "on");
    paint([ask, { client_id: "c1", role: "assistant", content: "Twelve boats.", live: true, created_at: "2026-11-07T18:00:12Z" }]);
    expect(speakSpy).not.toHaveBeenCalled();
    act(() => {
      queryClient.setQueryData(["chat", "bot-1", "chat-1"], chat([ask, answer("a1", "Twelve boats.")]));
    });
    await waitFor(() => expect(speakSpy).toHaveBeenCalledTimes(1));
    expect(made[0]?.text).toBe("Twelve boats.");
  });

  it("stays quiet while the toggle is off", async () => {
    paint([ask, answer("a1", "Twelve boats.")]);
    act(() => {
      queryClient.setQueryData(
        ["chat", "bot-1", "chat-1"],
        chat([ask, answer("a1", "Twelve boats."), answer("a2", "Nine ships.")]),
      );
    });
    await act(async () => undefined);
    expect(speakSpy).not.toHaveBeenCalled();
  });

  it("reads aloud through the server voice when the browser has none", async () => {
    removeVoice();
    installServerVoice(200);
    localStorage.setItem("easyagent.readAloud", "on");
    paint([ask, answer("a1", "Twelve boats.")]);
    // The Speak row appearing proves the voices probe proved the server can speak.
    await waitFor(() => expect(screen.getByTestId("speak-button")).toBeTruthy());
    act(() => {
      queryClient.setQueryData(
        ["chat", "bot-1", "chat-1"],
        chat([ask, answer("a1", "Twelve boats."), answer("a2", "Nine ships.")]),
      );
    });
    await waitFor(() => expect(playSpy).toHaveBeenCalledTimes(1));
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/bots/bot-1/tts",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ text: "Nine ships." }) }),
    );
    expect(speakSpy).not.toHaveBeenCalled();
  });
});
