import { describe, expect, it } from "vitest";
import { localWhen } from "./time";

describe("local time", () => {
  it("turns a UTC stamp into a clock the reader can scan", () => {
    expect(localWhen("")).toBe("undated");
    expect(localWhen("not a date")).toBe("not a date");
    expect(localWhen("2026-10-08T19:30:00+00:00", "America/New_York")).toBe("Oct 8, 2026, 3:30 PM");
  });
});