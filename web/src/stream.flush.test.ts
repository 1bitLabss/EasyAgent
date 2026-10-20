import { describe, expect, it } from "vitest";
import { MAX_STREAM_FLUSH_GAP_MS, STREAM_DELTA_FLUSH_MS, streamFlushDelay } from "@/stream";

describe("stream flush floor", () => {
  it("waits out the 33ms floor and stretches with the last flush cost", () => {
    expect(streamFlushDelay(0, 0)).toBe(STREAM_DELTA_FLUSH_MS);
    expect(streamFlushDelay(40, 0)).toBe(0);
    expect(streamFlushDelay(10, 0)).toBe(23);
    expect(streamFlushDelay(0, 20)).toBe(60);
    expect(streamFlushDelay(0, 200)).toBe(MAX_STREAM_FLUSH_GAP_MS);
    expect(streamFlushDelay(100, 200)).toBe(150);
  });
});
