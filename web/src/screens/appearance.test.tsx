import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { useApp } from "@/store";
import { AppearanceBlock } from "./Settings";

vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, networkMode: "always" } } });
  return {
    queryClient: client,
    api: vi.fn(async () => ({})),
  };
});

const ACCENT_CLASSES = ["accent-blue", "accent-green", "accent-violet", "accent-amber", "accent-rose"];

function wipeLook() {
  localStorage.clear();
  document.documentElement.classList.remove("dark", "text-large", ...ACCENT_CLASSES);
  useApp.setState({ theme: "light", accent: "default", textSize: "normal" });
}

afterEach(() => {
  cleanup();
  queryClient.clear();
});

beforeEach(() => {
  wipeLook();
});

function paint() {
  render(
    <QueryClientProvider client={queryClient}>
      <AppearanceBlock />
    </QueryClientProvider>,
  );
}

describe("appearance settings", () => {
  it("picking an accent swatch saves it and puts exactly one accent class on <html>", () => {
    paint();
    fireEvent.click(screen.getByTestId("accent-swatch-blue"));
    expect(localStorage.getItem("easyagent.accent")).toBe("blue");
    expect(document.documentElement.classList.contains("accent-blue")).toBe(true);
    // Switching colors removes the previous class; never two accents at once.
    fireEvent.click(screen.getByTestId("accent-swatch-green"));
    expect(localStorage.getItem("easyagent.accent")).toBe("green");
    expect(document.documentElement.classList.contains("accent-green")).toBe(true);
    expect(document.documentElement.classList.contains("accent-blue")).toBe(false);
  });

  it("picking the Default swatch stores default and removes every accent class", () => {
    paint();
    fireEvent.click(screen.getByTestId("accent-swatch-violet"));
    expect(document.documentElement.classList.contains("accent-violet")).toBe(true);
    fireEvent.click(screen.getByTestId("accent-swatch-default"));
    expect(localStorage.getItem("easyagent.accent")).toBe("default");
    expect(ACCENT_CLASSES.some((name) => document.documentElement.classList.contains(name))).toBe(false);
  });

  it("a stored accent is applied when the store initializes", async () => {
    localStorage.setItem("easyagent.accent", "violet");
    vi.resetModules();
    const store = await import("@/store");
    expect(store.useApp.getState().accent).toBe("violet");
    expect(document.documentElement.classList.contains("accent-violet")).toBe(true);
    vi.resetModules();
  });

  it("an unknown stored accent falls back to default with no class", async () => {
    localStorage.setItem("easyagent.accent", "chartreuse");
    vi.resetModules();
    const store = await import("@/store");
    expect(store.useApp.getState().accent).toBe("default");
    expect(ACCENT_CLASSES.some((name) => document.documentElement.classList.contains(name))).toBe(false);
    vi.resetModules();
  });

  it("Large text saves the key and raises the root size class", () => {
    paint();
    fireEvent.click(screen.getByTestId("text-size-large"));
    expect(localStorage.getItem("easyagent.textSize")).toBe("large");
    expect(document.documentElement.classList.contains("text-large")).toBe(true);
  });

  it("switching back to Normal removes the text-large class", () => {
    paint();
    fireEvent.click(screen.getByTestId("text-size-large"));
    fireEvent.click(screen.getByTestId("text-size-normal"));
    expect(localStorage.getItem("easyagent.textSize")).toBe("normal");
    expect(document.documentElement.classList.contains("text-large")).toBe(false);
  });

  it("the Mode buttons flip the dark class and persist the choice", () => {
    paint();
    fireEvent.click(screen.getByTestId("mode-dark"));
    expect(localStorage.getItem("easyagent.theme")).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    fireEvent.click(screen.getByTestId("mode-light"));
    expect(localStorage.getItem("easyagent.theme")).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });
});
