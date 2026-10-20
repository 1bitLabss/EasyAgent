import { describe, expect, it } from "vitest";
import { splitStreamingMarkdown } from "./markdown";

describe("splitStreamingMarkdown", () => {
  it("keeps closed paragraphs and caps the open tail", () => {
    const text = `${"alpha beta.\n\n".repeat(40)}${"x".repeat(5000)}`;
    const parts = splitStreamingMarkdown(text, 2000);
    expect(parts.closed.endsWith("\n\n") || parts.closed.length > 0).toBe(true);
    expect(parts.tail.length).toBeLessThanOrEqual(2000);
    expect(parts.closed + parts.tail).toBe(text);
  });
});
