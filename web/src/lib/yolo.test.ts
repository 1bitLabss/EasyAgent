import { describe, expect, it } from "vitest";
import { yoloClock, yoloRemaining } from "./yolo";

describe("yolo clock", () => {
  it("formats hours, minutes, and seconds", () => {
    expect(yoloClock(2 * 3600 + 12 * 60)).toBe("2h 12m");
    expect(yoloClock(3600)).toBe("1h");
    expect(yoloClock(12 * 60)).toBe("12m");
    expect(yoloClock(45)).toBe("45s");
  });

  it("counts down from the payload and reaches zero", () => {
    const yolo = { on: true, remaining_seconds: 90 };
    expect(yoloRemaining(yolo, 1_000, 1_000)).toBe(90);
    expect(yoloRemaining(yolo, 1_000, 31_000)).toBe(60);
    expect(yoloRemaining(yolo, 1_000, 91_000)).toBe(0);
    expect(yoloRemaining({ on: false, remaining_seconds: 90 }, 1_000, 1_000)).toBe(0);
  });
});
