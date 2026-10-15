import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, queryClient } from "@/api";
import { ApprovalCard, type Approval } from "./ApprovalCard";
import { AddBotForm } from "./Sidebar";

let approvals: Approval[] = [];

vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, networkMode: "always" } } });
  return {
    queryClient: client,
    api: vi.fn(async (path: string, options?: { method?: string; json?: unknown }) => {
      if (path === "/api/endpoints" && !options?.method) return [];
      if (path === "/api/bots" && options?.method === "POST") return { id: "b1", name: "Ada" };
      if (path === "/api/bots/b1/ongoing") return { id: "c1" };
      if (path.endsWith("/approvals") && !options?.method) return approvals;
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

function card(overrides: Partial<Approval>): Approval {
  return {
    id: "card1",
    bot_id: "b1",
    tier: "ask",
    rule: "unlisted-command",
    why: "It waits for you.",
    detail: "pytest -q",
    offer_always: true,
    proposal: null,
    ...overrides,
  };
}

describe("Add a bot", () => {
  it("adds a bot with a name and no connection, without creating a connection", async () => {
    const { findByTestId, getByPlaceholderText, getByText } = render(
      <QueryClientProvider client={queryClient}>
        <AddBotForm onClose={() => undefined} />
      </QueryClientProvider>,
    );
    const select = (await findByTestId("add-bot-connection")) as HTMLSelectElement;
    expect(select.value).toBe("");
    fireEvent.change(getByPlaceholderText("Ada"), { target: { value: "Ada" } });
    fireEvent.click(getByText("Add bot"));
    await waitFor(() => expect(mocked.mock.calls.some(([path, options]) => path === "/api/bots" && options?.method === "POST")).toBe(true));
    const post = mocked.mock.calls.find(([path, options]) => path === "/api/bots" && options?.method === "POST");
    expect(post?.[1]?.json).toMatchObject({ name: "Ada", endpoint_id: null });
    expect(mocked.mock.calls.some(([path, options]) => path === "/api/endpoints" && options?.method === "POST")).toBe(false);
  });
});

describe("Approval card lasting grants", () => {
  async function buttons(next: Approval) {
    approvals = [next];
    const view = render(
      <QueryClientProvider client={queryClient}>
        <ApprovalCard botId="b1" botName="Ada" active />
      </QueryClientProvider>,
    );
    await view.findByTestId("approval-card");
    return Array.from(view.container.querySelectorAll("button")).map((node) => node.textContent || "");
  }

  it("offers Always allow on an ordinary card that the server marked", async () => {
    expect(await buttons(card({}))).toContain("Always allow this command for this bot");
  });

  it("offers the action class by name and does not offer it on a blocked rule", async () => {
    const label = "Allow all: read files in this bot's folder for this chat";
    const labels = await buttons(card({ class_label: label, action_class: "read-in-workspace" }));
    expect(labels).toContain(label);
    cleanup();
    queryClient.clear();
    const hidden = await buttons(card({
      rule: "bridge-send",
      class_label: label,
      action_class: "read-in-workspace",
      proposal: { kind: "script-diff", session: true, exec_always: true },
    }));
    expect(hidden).not.toContain(label);
  });

  it.each(["bridge-send", "browser-submit", "mcp-ask", "own-server", "root-delete", "guardrail-edit"])(
    "never offers a lasting grant for %s, even if the flags say so",
    async (rule) => {
      const labels = await buttons(card({ rule, offer_always: true, proposal: { kind: "script-diff", session: true, exec_always: true } }));
      expect(labels.some((label) => label.startsWith("Always allow"))).toBe(false);
      expect(labels).not.toContain("Allow for this session");
    },
  );

  it("never offers a lasting grant on a blocked-tier card", async () => {
    const labels = await buttons(card({ tier: "block", offer_always: true, proposal: { kind: "script-diff", session: true, exec_always: true } }));
    expect(labels.some((label) => label.startsWith("Always allow"))).toBe(false);
    expect(labels).not.toContain("Allow for this session");
  });

  it("offers session and Always allow on a script card only when the server says so", async () => {
    const both = await buttons(card({ rule: "bot-changed", offer_always: false, proposal: { kind: "script-diff", session: true, exec_always: true } }));
    expect(both).toContain("Allow for this session");
    expect(both).toContain("Always allow");
    cleanup();
    queryClient.clear();
    const none = await buttons(card({ rule: "bot-changed", offer_always: false, proposal: { kind: "script-diff", session: true, exec_always: false } }));
    expect(none).toContain("Allow for this session");
    expect(none).not.toContain("Always allow");
  });
});

describe("local server queries", async () => {
  const real = await vi.importActual<typeof import("@/api")>("@/api");
  it("run even when the browser thinks it is offline", () => {
    expect(real.queryClient.getDefaultOptions().queries?.networkMode).toBe("always");
    expect(real.queryClient.getDefaultOptions().mutations?.networkMode).toBe("always");
  });
});
