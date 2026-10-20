import { createElement } from "react";
import { renderToString } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { describe, expect, it } from "vitest";
import { splitStreamingMarkdown } from "./markdown";

describe("streaming markdown cost", () => {
  it("re-parses only the tail, under 16ms at 16k characters", () => {
    let body = "";
    let index = 0;
    while (body.length < 16100) {
      body += `Paragraph ${index}. ${"word ".repeat(40)}\n\n`;
      index += 1;
    }
    const text = `${body}token`.slice(0, 16000);
    const parts = splitStreamingMarkdown(text);
    expect(parts.closed + parts.tail).toBe(text);
    expect(parts.tail.length).toBeLessThanOrEqual(2000);
    const started = performance.now();
    renderToString(createElement(ReactMarkdown, { remarkPlugins: [remarkGfm] }, parts.tail));
    const cost = performance.now() - started;
    expect(cost).toBeLessThan(16);
  });
});
