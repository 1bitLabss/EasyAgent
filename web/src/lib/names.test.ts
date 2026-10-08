import { describe, expect, it } from "vitest";
import { foldName, namesMatch } from "./names";

describe("names", () => {
  it("matches a name when the case or spacing differs", () => {
    expect(foldName("  bEn ")).toBe("ben");
    expect(foldName("kiln   notes")).toBe("kiln notes");
    expect(namesMatch("  aDa ", "Ada")).toBe(true);
    expect(namesMatch("  workSHOP ", "Workshop")).toBe(true);
    expect(namesMatch("Bea", "Ada")).toBe(false);
    expect(namesMatch("   ", "Ada")).toBe(false);
    expect(namesMatch("Ada", "")).toBe(false);
  });
});
