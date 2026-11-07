import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { queryClient } from "@/api";
import { useApp } from "@/store";
import { JobsPanel, type LiveChildRow } from "./JobsPanel";

const BOT_ID = "ada-bot";
const CHAT_ID = "ada-chat";

let children: LiveChildRow[] = [];
let routines: unknown[] = [];

vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnWindowFocus: false } },
  });
  return {
    queryClient: client,
    api: vi.fn(async (path: string) => {
      if (path === "/api/children") return children;
      if (path === "/api/routines") return routines;
      return {};
    }),
  };
});

function paint() {
  render(
    <QueryClientProvider client={queryClient}>
      <JobsPanel />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  queryClient.clear();
  children = [];
  routines = [];
  useApp.setState({ botId: null, chatId: null, screen: "chat" });
});

describe("jobs panel working-now section", () => {
  it("lists the children working now and opens the child chat", async () => {
    children = [
      {
        child_id: "c1",
        name: "Scout",
        task_excerpt: "count the boats",
        bot_id: BOT_ID,
        chat_id: CHAT_ID,
        started_at: (Date.now() - 30_000) / 1000,
      },
    ];
    paint();

    const row = await screen.findByTestId("working-child-row");
    expect(row.textContent).toContain("Scout");
    expect(row.textContent).toContain("count the boats");
    expect(row.textContent).toContain("30s");

    await act(async () => {
      fireEvent.click(row.querySelector("button") as HTMLButtonElement);
    });
    expect(useApp.getState().botId).toBe(BOT_ID);
    expect(useApp.getState().chatId).toBe(CHAT_ID);
  });

  it("says nothing is working when the board is empty", async () => {
    children = [];
    routines = [];
    paint();

    const empty = await screen.findByTestId("working-now-empty");
    expect(empty.textContent).toBe("Nothing is working.");
    expect(screen.queryAllByTestId("working-child-row").length).toBe(0);
  });
});
