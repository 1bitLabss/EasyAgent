import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, queryClient } from "@/api";
import { DirectionScreen } from "./Places";

vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, networkMode: "always" } } });
  return {
    queryClient: client,
    api: vi.fn(async (path: string, options?: { method?: string; json?: { text?: string } }) => {
      if (path === "/api/bots") return [{ id: "b1", name: "Ada" }, { id: "b2", name: "Bea" }];
      if (path === "/api/direction" && options?.method === "PUT") return { text: `${options.json?.text}\n` };
      if (path === "/api/direction") return { text: "Shared note." };
      if (path === "/api/direction?bot_id=b1") return { text: "Ada's own note.", own: true };
      if (path === "/api/direction?bot_id=b2") return { text: "Shared note.", own: false };
      return {};
    }),
  };
});

const mocked = vi.mocked(api);

afterEach(() => {
  cleanup();
  mocked.mockClear();
  queryClient.clear();
});

describe("Direction screen per-bot note", () => {
  it("shows a bot picker fed by the bot list", async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <DirectionScreen />
      </QueryClientProvider>,
    );
    const picker = (await screen.findByTestId("bot-direction-select")) as HTMLSelectElement;
    await waitFor(() => expect(picker.querySelector('option[value="b1"]')?.textContent).toBe("Ada"));
    expect(picker.querySelector('option[value="b2"]')?.textContent).toBe("Bea");
  });

  it("loads the bot's own note and saves it to the bot-scoped URL", async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <DirectionScreen />
      </QueryClientProvider>,
    );
    const picker = await screen.findByTestId("bot-direction-select");
    await waitFor(() => expect(picker.querySelector('option[value="b1"]')).not.toBeNull());
    fireEvent.change(picker, { target: { value: "b1" } });
    await waitFor(() =>
      expect((screen.getByTestId("bot-direction-text") as HTMLTextAreaElement).value).toBe("Ada's own note."),
    );
    const box = screen.getByTestId("bot-direction-text") as HTMLTextAreaElement;
    expect((await screen.findByTestId("bot-direction-state")).textContent).toContain("has its own note");
    fireEvent.change(box, { target: { value: "Ada: answer in French." } });
    fireEvent.click(screen.getByTestId("bot-direction-save"));
    await waitFor(() =>
      expect(mocked).toHaveBeenCalledWith("/api/direction?bot_id=b1", {
        method: "PUT",
        json: { text: "Ada: answer in French." },
      }),
    );
    expect(mocked.mock.calls.some(([path, options]) => path === "/api/direction" && options?.method === "PUT")).toBe(false);
  });

  it("says plainly when a bot follows the shared note", async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <DirectionScreen />
      </QueryClientProvider>,
    );
    const picker = await screen.findByTestId("bot-direction-select");
    await waitFor(() => expect(picker.querySelector('option[value="b2"]')).not.toBeNull());
    fireEvent.change(picker, { target: { value: "b2" } });
    const state = await screen.findByTestId("bot-direction-state");
    expect(state.textContent).toContain("no note of its own");
    await waitFor(() =>
      expect((screen.getByTestId("bot-direction-text") as HTMLTextAreaElement).value).toBe("Shared note."),
    );
  });

  it("still saves the shared note to the plain URL", async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <DirectionScreen />
      </QueryClientProvider>,
    );
    const box = await screen.findByTestId("direction-text");
    fireEvent.change(box, { target: { value: "Shared rule changed." } });
    fireEvent.click(screen.getByTestId("direction-save"));
    await waitFor(() =>
      expect(mocked).toHaveBeenCalledWith("/api/direction", {
        method: "PUT",
        json: { text: "Shared rule changed." },
      }),
    );
  });
});
