import { expect, it } from "vitest";
import { applyVisualViewport } from "@/lib/viewport";

it("pins the frame to the visible viewport so the keyboard stays below the composer", () => {
  const el = document.createElement("html");
  applyVisualViewport({ height: 420.4, offsetTop: 80.2 }, el);
  expect(el.style.getPropertyValue("--app-height")).toBe("420px");
  expect(el.style.getPropertyValue("--vv-offset")).toBe("80px");
});

it("ignores a missing viewport", () => {
  const el = document.createElement("html");
  applyVisualViewport(null, el);
  expect(el.style.getPropertyValue("--app-height")).toBe("");
});
