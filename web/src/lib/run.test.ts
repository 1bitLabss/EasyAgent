import { describe, expect, it } from "vitest";
import { moodForBadge, moodForReaction } from "./mood";
import { contextNote, describeRun, faceStateFor, runTone } from "./run";

describe("run tone", () => {
  it("names waiting, reconnecting, tools, and a halt", () => {
    expect(runTone("Queued: Home server", "running")).toBe("waiting");
    expect(runTone("Model not answering, retrying…", "running")).toBe("reconnecting");
    expect(runTone("Searching the web", "running")).toBe("tool");
    expect(runTone("Thinking", "running")).toBe("thinking");
    expect(runTone("Thinking", "stopped")).toBe("halted");
  });

  it("talks once text is on screen and stays idle when nothing is running", () => {
    expect(faceStateFor({ sending: true, text: "Hi", runStatus: "running", label: "Thinking" })).toBe("talking");
    expect(faceStateFor({ sending: true, label: "Model not answering", runStatus: "running" })).toBe("reconnecting");
    expect(faceStateFor({})).toBe("idle");
    expect(faceStateFor({ busy: true })).toBe("thinking");
    expect(faceStateFor({ phase: "error" })).toBe("halted");
  });

  it("says it is still waiting after a quiet minute", () => {
    const now = Date.parse("2026-10-08T12:01:05Z");
    const view = describeRun("Thinking", "running", "2026-10-08T12:00:00Z", "2026-10-08T12:00:00Z", undefined, now);
    expect(view.words).toBe("still waiting on the model (65s)");
    expect(view.elapsed).toBe("1m 5s");
  });

  it("maps a passed check to a glad face and a failed check to a sad one", () => {
    expect(moodForBadge("checked")).toBe("glad");
    expect(moodForBadge("revised")).toBe("sad");
    expect(moodForBadge("")).toBeNull();
    expect(moodForReaction("👍")).toBe("glad");
    expect(moodForReaction("❤️")).toBe("glad");
    expect(moodForReaction("👎")).toBe("sad");
  });

  it("describes an empty chat budget in plain words", () => {
    expect(contextNote({ max_context_tokens: 24000 }, "chat")).toContain("No messages yet");
  });
});
