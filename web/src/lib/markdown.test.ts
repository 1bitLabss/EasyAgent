import { describe, expect, it } from "vitest";
import { settledBlocks, splitStreamingMarkdown } from "./markdown";

describe("splitStreamingMarkdown", () => {
  it("keeps closed paragraphs and caps the open tail", () => {
    const text = `${"alpha beta.\n\n".repeat(40)}${"x".repeat(5000)}`;
    const parts = splitStreamingMarkdown(text, 2000);
    expect(parts.closed.endsWith("\n\n") || parts.closed.length > 0).toBe(true);
    expect(parts.tail.length).toBeLessThanOrEqual(2000);
    expect(parts.closed + parts.tail).toBe(text);
  });

  it("keeps earlier paragraphs when a later one arrives", () => {
    const first = settledBlocks("one\n\ntwo\n\n");
    const next = settledBlocks("one\n\ntwo\n\nthree\n\n");
    expect(next.slice(0, first.length)).toEqual(first);
    expect(next.join("")).toBe("one\n\ntwo\n\nthree\n\n");
  });

  it("does not split inside a code fence", () => {
    const text = "before\n\n```\nline\n\nline\n```\n\nafter\n\n";
    const blocks = settledBlocks(text);
    expect(blocks.join("")).toBe(text);
    expect(blocks.some((block) => block.includes("line\n\nline"))).toBe(true);
  });
});
