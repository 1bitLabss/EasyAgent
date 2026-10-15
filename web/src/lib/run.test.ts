import { describe, expect, it } from "vitest";
import { moodForBadge, moodForReaction } from "./mood";
import { activityLine, contextNote, describeRun, faceStateFor, runPresence, runTone, workingLine } from "./run";

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

  it("names idle, working, and waiting on the model", () => {
    const start = Date.parse("2026-10-08T12:00:00Z");
    expect(runPresence({ active: false, now: start }).text).toBe("Idle");
    expect(runPresence({
      active: true,
      step: "Waiting on model",
      startedAt: "2026-10-08T12:00:00Z",
      now: start + 8_000,
    })).toEqual({ kind: "waiting", text: "Waiting on model, 8s" });
    expect(runPresence({
      active: true,
      step: "Reading index.html",
      startedAt: "2026-10-08T12:00:00Z",
      now: start + 12_000,
    })).toEqual({ kind: "working", text: "Working, 12s · Reading index.html" });
  });

  it("names a live wait and a running command", () => {
    const start = Date.parse("2026-10-08T12:00:00Z");
    const now = start + (3 * 60 + 10) * 1000;
    expect(activityLine({
      step: "Waiting on model",
      activityStartedAt: "2026-10-08T12:00:00Z",
      now,
      tokens: 1200,
    })).toBe("Waiting on model, 3m 10s, 1.2k tokens so far");
    expect(activityLine({
      step: "Running npm install",
      activityStartedAt: "2026-10-08T12:00:00Z",
      now: start + 42_000,
    })).toBe("Running npm install, 42s");
    expect(activityLine({ step: "Writing index.html", activityStartedAt: "2026-10-08T12:00:00Z", now: start })).toBe("Writing index.html, 0s");
    expect(runTone("Writing index.html", "running")).toBe("tool");
    expect(runTone("Waiting on model, 3m 10s, 1.2k tokens so far", "running")).toBe("waiting");
  });

  it("names the step, the elapsed time, and a queued message", () => {
    const now = Date.parse("2026-10-08T12:01:05Z");
    const view = workingLine(
      { step_index: 2, step_count: 5, started_at: "2026-10-08T12:00:00Z", current_step: "Reading notes", queued: 1, stuck: true },
      now,
    );
    expect(view.text).toBe("Step 2 of 5 · 1m 5s · Reading notes");
    expect(view.queued).toBe(true);
    expect(view.stuck).toBe(true);
  });

  it("shows a reconnect instead of the last tool step", () => {
    const now = Date.parse("2026-10-08T12:02:00Z");
    const view = workingLine(
      {
        step_index: 2,
        step_count: 5,
        started_at: "2026-10-08T12:00:00Z",
        current_step: "Model not answering, retrying (attempt 4)…",
        last_activity: "Reading notes",
        stuck: true,
      },
      now,
    );
    expect(view.text).toBe("2m 0s · Model not answering, retrying (attempt 4)…");
    expect(view.stuck).toBe(false);
    const quiet = describeRun(
      "Model not answering, retrying (attempt 4)…",
      "running",
      "2026-10-08T12:00:00Z",
      "2026-10-08T12:00:00Z",
      undefined,
      now,
    );
    expect(quiet.words).toBe("Model not answering, retrying (attempt 4)…");
  });

  it("describes an empty chat budget in plain words", () => {
    expect(contextNote({ max_context_tokens: 24000 }, "chat")).toContain("No messages yet");
  });
});
