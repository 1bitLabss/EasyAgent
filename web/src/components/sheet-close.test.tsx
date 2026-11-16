import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Sheet } from "./Sheet";

afterEach(() => {
  cleanup();
});

function openSheet() {
  const onClose = vi.fn();
  render(
    <Sheet title="Settings" onClose={onClose}>
      <p>sheet body</p>
    </Sheet>,
  );
  return onClose;
}

describe("Sheet close paths", () => {
  it("closes when the backdrop behind the panel is clicked", () => {
    const onClose = openSheet();
    fireEvent.click(screen.getByTestId("sheet-backdrop"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes when Escape is pressed", () => {
    const onClose = openSheet();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("still closes from the X button", () => {
    const onClose = openSheet();
    fireEvent.click(screen.getAllByLabelText("Close")[0] as HTMLElement);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("ignores other keys", () => {
    const onClose = openSheet();
    fireEvent.keyDown(window, { key: "Enter" });
    expect(onClose).not.toHaveBeenCalled();
  });
});
