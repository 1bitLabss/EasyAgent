import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { useApp } from "@/store";
import { resetStreamForTests } from "@/stream";
import { stopSpeaking } from "@/lib/speak";
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
});

afterEach(() => {
  cleanup();
  resetStreamForTests();
  queryClient.clear();
  useApp.setState({ botId: null, chatId: null, screen: "chat" });
  memoryStore.clear();
  stopSpeaking();
  removeVoice();
  speakSpy.mockClear();
  cancelSpy.mockClear();
  made.length = 0;
});

describe("the quiet speak control", () => {
  it("speaks the reply with markdown stripped", () => {
    paint([ask, answer("a1", "**Twelve** boats.\n\n```python\nprint(1)\n```\n\nThat is all.")]);
    fireEvent.click(screen.getByTestId("speak-button"));
    expect(speakSpy).toHaveBeenCalledTimes(1);
    expect(made[0]?.text).toBe("Twelve boats. That is all.");
  });

  it("clicking again stops instead of speaking twice", () => {
    paint([ask, answer("a1", "Twelve boats.")]);
    cancelSpy.mockClear();
    const button = screen.getByTestId("speak-button");
    fireEvent.click(button);
    expect(button.textContent).toBe("Stop");
    fireEvent.click(button);
    expect(speakSpy).toHaveBeenCalledTimes(1);
    expect(cancelSpy).toHaveBeenCalledTimes(2);
    expect(button.textContent).toBe("Speak");
  });

  it("starting another message cancels the first", () => {
    paint([ask, answer("a1", "Twelve boats."), answer("a2", "Nine ships.")]);
    cancelSpy.mockClear();
    const buttons = screen.getAllByTestId("speak-button");
    fireEvent.click(buttons[0]);
    fireEvent.click(buttons[1]);
    expect(speakSpy).toHaveBeenCalledTimes(2);
    expect(cancelSpy).toHaveBeenCalledTimes(2);
    expect(made.map((utterance) => utterance.text)).toEqual(["Twelve boats.", "Nine ships."]);
  });

  it("switching chats cancels mid-sentence", () => {
    paint([ask, answer("a1", "Twelve boats.")]);
    fireEvent.click(screen.getByTestId("speak-button"));
    cancelSpy.mockClear();
    act(() => {
      useApp.setState({ chatId: "chat-2" });
    });
    expect(cancelSpy).toHaveBeenCalled();
  });

  it("stays hidden when the webview has no speechSynthesis", () => {
    removeVoice();
    paint([ask, answer("a1", "Twelve boats.")]);
    expect(screen.queryByTestId("speak-button")).toBeNull();
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
});
