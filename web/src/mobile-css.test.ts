// Phone-width CSS guards (FIX: 13px checkboxes, iOS text inflation / rubber-band,
// composer squeezed to ~120px at 360px). jsdom can't compute media queries, so —
// like the reaction-css guard in ChatPane.test.tsx — these assert the shipped rules.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(resolve("src/index.css"), "utf8");

function blockInside(mediaHeader: string, selectorText: string): string | null {
  const start = css.indexOf(mediaHeader);
  if (start < 0) return null;
  const end = css.indexOf("@media", start + mediaHeader.length);
  const scope = css.slice(start, end < 0 ? css.length : end);
  const rule = scope.indexOf(selectorText);
  if (rule < 0) return null;
  const close = scope.indexOf("}", rule);
  return scope.slice(rule, close < 0 ? scope.length : close);
}

describe("phone tap targets and sizing", () => {
  it("gives checkboxes a thumb-sized hit area at phone width", () => {
    const block = blockInside("@media (max-width: 980px)", 'input[type="checkbox"]');
    expect(block).not.toBeNull();
    expect(block).toMatch(/min-width:\s*28px/);
    expect(block).toMatch(/min-height:\s*28px/);
  });

  it("stops iOS landscape text inflation and rubber-band scroll chaining", () => {
    expect(css).toMatch(/-webkit-text-size-adjust:\s*100%/);
    expect(css).toMatch(/overscroll-behavior:\s*none/);
  });

  it("lets the composer textarea win width on small phones", () => {
    const hidden = blockInside("@media (max-width: 400px)", ".composer .stop-send-slot:not(.is-on)");
    expect(hidden).not.toBeNull();
    expect(hidden).toMatch(/display:\s*none/);
    const shrunk = blockInside("@media (max-width: 400px)", ".composer .stop-send-slot.is-on");
    expect(shrunk).not.toBeNull();
  });
});
