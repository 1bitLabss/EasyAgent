import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The composer's server-STT fallback: browsers with no SpeechRecognition
// (Safari, Firefox) record with MediaRecorder and the server turns the
// recording into words. The api module is mocked: no real upload happens.
vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  return {
    queryClient: new QueryClient({ defaultOptions: { queries: { retry: false } } }),
    api: vi.fn(),
    transcribeAudio: vi.fn(async () => "order two pizzas"),
  };
});

type RecorderLike = {
  ondataavailable: ((event: { data: Blob }) => void) | null;
  onstop: (() => void) | null;
  onerror: (() => void) | null;
  started: boolean;
  start: () => void;
  stop: () => void;
};

const recorders: RecorderLike[] = [];

function makeRecorderClass() {
  return class FakeRecorder implements RecorderLike {
    ondataavailable: ((event: { data: Blob }) => void) | null = null;
    onstop: (() => void) | null = null;
    onerror: (() => void) | null = null;
    started = false;
    constructor(public stream: MediaStream) {
      recorders.push(this);
    }
    start() {
      this.started = true;
    }
    stop() {
      this.ondataavailable?.({ data: new Blob(["voice-bytes"]) });
      this.onstop?.();
    }
  };
}

const track = { stop: vi.fn() };
const stream = { getTracks: () => [track] } as unknown as MediaStream;
const getUserMedia = vi.fn(async () => stream);

let Composer: typeof import("@/components/Composer").Composer;
let transcribeAudioMock: ReturnType<typeof vi.fn>;
let onError: ReturnType<typeof vi.fn>;
let fetchSpy: ReturnType<typeof vi.fn>;

async function load() {
  vi.resetModules();
  ({ Composer } = await import("@/components/Composer"));
  const apiMod = await import("@/api");
  transcribeAudioMock = vi.mocked(apiMod.transcribeAudio);
}

function paint() {
  onError = vi.fn();
  render(
    <Composer
      running={false}
      asking={false}
      error=""
      resumeRetry={false}
      resumeCont={false}
      onSend={vi.fn()}
      onStop={vi.fn()}
      onStopAndSend={vi.fn()}
      onRetry={vi.fn()}
      onContinue={vi.fn()}
      onError={onError}
      botId="bot-1"
      chatId="chat-1"
      getHistory={() => []}
    />,
  );
}

function mic() {
  return screen.getByLabelText("Microphone");
}

function draft() {
  return screen.getByLabelText("Message") as HTMLTextAreaElement;
}

beforeEach(async () => {
  await load();
  Object.defineProperty(window.navigator, "mediaDevices", {
    value: { getUserMedia },
    configurable: true,
  });
  (window as { MediaRecorder?: unknown }).MediaRecorder = makeRecorderClass();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete (window as { SpeechRecognition?: unknown }).SpeechRecognition;
  delete (window as { webkitSpeechRecognition?: unknown }).webkitSpeechRecognition;
  recorders.length = 0;
  getUserMedia.mockClear();
  fetchSpy = vi.fn();
});

describe("composer microphone with server transcription", () => {
  it("keeps the browser's own speech recognition when it exists and never asks the server", async () => {
    const ears: { start: ReturnType<typeof vi.fn> }[] = [];
    (window as { SpeechRecognition?: unknown }).SpeechRecognition = class {
      lang = "";
      onresult: unknown = null;
      onend: unknown = null;
      start = vi.fn(() => undefined);
      constructor() {
        ears.push(this as { start: ReturnType<typeof vi.fn> });
      }
    };
    fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    paint();
    fireEvent.click(mic());
    expect(ears[0].start).toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(getUserMedia).not.toHaveBeenCalled();
  });

  it("records with the server's help and puts the words in the message box", async () => {
    fetchSpy = vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({ enabled: true, base_url: "http://127.0.0.1:9/v1", model: "whisper-1", has_key: false }),
      })),
    ) as unknown as ReturnType<typeof vi.fn>;
    paint();
    await waitFor(() => {
      fireEvent.click(mic());
      expect(getUserMedia).toHaveBeenCalled();
    });
    await waitFor(() => expect(recorders[0]?.started).toBe(true));
    expect(draft().placeholder).toBe("Listening");
    fireEvent.click(mic());
    await waitFor(() => expect(draft().value).toBe("order two pizzas"));
    expect(transcribeAudioMock).toHaveBeenCalledTimes(1);
    expect(track.stop).toHaveBeenCalled();
  });

  it("appends the words to what the person already typed", async () => {
    fetchSpy = vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({ enabled: true, base_url: "http://127.0.0.1:9/v1", model: "whisper-1", has_key: false }),
      })),
    ) as unknown as ReturnType<typeof vi.fn>;
    paint();
    fireEvent.change(draft(), { target: { value: "buy milk" } });
    await waitFor(() => {
      fireEvent.click(mic());
      expect(getUserMedia).toHaveBeenCalled();
    });
    await waitFor(() => expect(recorders[0]?.started).toBe(true));
    fireEvent.click(mic());
    await waitFor(() => expect(draft().value).toBe("buy milk order two pizzas"));
  });

  it("still says the browser cannot use the microphone when the server has nothing set up", async () => {
    fetchSpy = vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({ enabled: false, base_url: "", model: "whisper-1", has_key: false }),
      })),
    ) as unknown as ReturnType<typeof vi.fn>;
    paint();
    fireEvent.click(mic());
    expect(onError).toHaveBeenCalledWith("This browser cannot use the microphone.");
    expect(getUserMedia).not.toHaveBeenCalled();
  });
});
