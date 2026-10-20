import { createElement } from "react";
import { renderToString } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { describe, expect, it } from "vitest";
import { splitStreamingMarkdown } from "./markdown";

describe("streaming markdown cost", () => {
  it("re-parses only the tail, under 16ms at 16k characters", () => {
    const body = Array.from({ length: 80 }, (_, index) => `Paragraph ${index}. ${"word ".repeat(30)}`).join("\n\n");
    const text = `${body}\n\nOne more token`.slice(0, 16000);
    expect(text.length).toBeGreaterThanOrEqual(16000);
    const parts = splitStreamingMarkdown(text);
    expect(parts.closed + parts.tail).toBe(text);
    expect(parts.tail.length).toBeLessThanOrEqual(2000);
    const started = performance.now();
    renderToString(createElement(ReactMarkdown, { remarkPlugins: [remarkGfm] }, parts.tail));
    const cost = performance.now() - started;
    expect(cost).toBeLessThan(16);
  });
});
