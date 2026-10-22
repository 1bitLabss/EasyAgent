import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ThinkingBox, stickToBottom, thoughtLabel } from "./ThinkingBox";

describe("thinking panel", () => {
  afterEach(() => cleanup());
  it("stays open after reasoning finishes so the transcript does not jump", () => {
    const view = render(<ThinkingBox text={"Looking at the question.\nThe check found nothing."} streaming seconds={12} />);
    expect(screen.getByTestId("thinking-body").textContent).toContain("Looking at the question.");
    expect(screen.getByTestId("thinking-body").textContent).toContain("The check found nothing.");
    expect(screen.getByRole("button", { name: /Thinking/ }).textContent).toContain("Thought for 12s");
    expect(screen.getByTestId("thinking-body").className).toContain("thinking-body");

    view.rerender(<ThinkingBox text={"Looking at the question.\nThe check found nothing."} streaming={false} seconds={12} />);
    expect(screen.getByTestId("thinking-body").textContent).toContain("The check found nothing.");
    fireEvent.click(screen.getByRole("button", { name: /Thinking/ }));
    expect(screen.queryByTestId("thinking-body")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Thinking/ }));
    expect(screen.getByTestId("thinking-body").textContent).toContain("The check found nothing.");
  });

  it("closes when the answer starts and stays closed after that reply settles", () => {
    const view = render(<ThinkingBox text={"Plan the check."} streaming />);
    expect(view.getByTestId("thinking-body").textContent).toContain("Plan the check.");
    view.rerender(<ThinkingBox text={"Plan the check."} streaming answering />);
    expect(view.queryByTestId("thinking-body")).toBeNull();
    expect(view.getByRole("button", { name: /Thinking/ }).getAttribute("aria-expanded")).toBe("false");
    view.rerender(<ThinkingBox text={"Plan the check."} streaming={false} answering />);
    expect(view.queryByTestId("thinking-body")).toBeNull();
  });

  it("starts collapsed when the thought was not streaming", () => {
    const view = render(<ThinkingBox text={"A finished thought."} streaming={false} seconds={12} />);
    expect(view.container.querySelector("[data-testid=thinking-body]")).toBeNull();
    expect(view.container.textContent).toContain("Thought for 12s");
    expect(view.container.textContent).not.toContain("A finished thought.");
  });

  it("does not render an empty thinking box", () => {
    const view = render(<ThinkingBox text={" \n "} />);
    expect(view.container.textContent).toBe("");
    view.rerender(<ThinkingBox text="" streaming />);
    expect(view.container.textContent).toBe("");
  });

  it("names the elapsed thought and sticks to the bottom until the reader scrolls up", () => {
    expect(thoughtLabel(12)).toBe("Thought for 12s");
    expect(thoughtLabel(65)).toBe("Thought for 1m 5s");
    expect(thoughtLabel(undefined)).toBe("");
    expect(stickToBottom(400, 300, 80)).toBe(true);
    expect(stickToBottom(400, 100, 80)).toBe(false);
  });
});
