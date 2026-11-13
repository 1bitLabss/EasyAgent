import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, queryClient } from "@/api";
import { useApp } from "@/store";
import { SetupGate } from "./Setup";

vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, networkMode: "always" } } });
  return { queryClient: client, api: vi.fn() };
});

const mocked = vi.mocked(api);

type World = {
  bots: unknown[];
  onboarded: boolean;
  servers: { kind: string; base_url: string; label: string; models: string[] }[];
  probeOk: boolean;
};

let world: World;

function installApi() {
  world = { bots: [], onboarded: false, servers: [], probeOk: true };
  mocked.mockImplementation((async (path: string, options?: { method?: string; json?: Record<string, unknown> }) => {
    if (path === "/api/bots" && !options?.method) return world.bots;
    if (path === "/api/setup" && options?.method === "PUT") {
      world.onboarded = Boolean((options.json as { onboarded: boolean }).onboarded);
      return { onboarded: world.onboarded };
    }
    if (path === "/api/setup") return { onboarded: world.onboarded };
    if (path === "/api/local-servers") return { servers: world.servers };
    if (path === "/api/endpoints/probe") {
      return world.probeOk ? { ok: true, detail: "The server answered.", models: ["remote-model"] } : { ok: false, detail: "unreachable" };
    }
    if (path === "/api/endpoints" && options?.method === "POST") return { id: "e1", name: "Ollama" };
    if (path === "/api/bots" && options?.method === "POST") return { id: "b1", name: (options.json as { name: string }).name };
    if (path === "/api/bots/b1/ongoing") return { id: "c1" };
    return {};
  }) as typeof api);
}

function renderGate() {
  return render(
    <QueryClientProvider client={queryClient}>
      <SetupGate />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  mocked.mockReset();
  queryClient.clear();
  useApp.setState({ botId: null, chatId: null, screen: "chat", nudge: "" });
});

describe("first-run wizard gate", () => {
  it("shows the wizard when not onboarded and there are no bots", async () => {
    installApi();
    world.servers = [{ kind: "ollama", base_url: "http://127.0.0.1:11434/v1", label: "Ollama", models: ["llama3", "mistral"] }];
    const { findByText } = renderGate();
    await findByText("Find your model server");
    await findByText("Ollama");
  });

  it("hides the wizard when bots exist", async () => {
    installApi();
    world.bots = [{ id: "b1", name: "Ada" }];
    const { queryByText } = renderGate();
    await waitFor(() => expect(mocked.mock.calls.some(([path]) => path === "/api/setup")).toBe(true));
    await waitFor(() => expect(queryByText("Find your model server")).toBeNull());
  });

  it("hides the wizard once the setup flag says onboarded", async () => {
    installApi();
    world.onboarded = true;
    const { queryByText } = renderGate();
    await waitFor(() => expect(mocked.mock.calls.some(([path]) => path === "/api/setup")).toBe(true));
    expect(queryByText("Find your model server")).toBeNull();
  });
});

describe("first-run wizard flow", () => {
  it("detects a server, picks a model, and creates endpoint, bot, and setup flag in order", async () => {
    installApi();
    world.servers = [{ kind: "ollama", base_url: "http://127.0.0.1:11434/v1", label: "Ollama", models: ["llama3", "mistral"] }];
    const { findByText, getByTestId, getByDisplayValue, getByText } = renderGate();
    await findByText("Find your model server");
    fireEvent.click(await findByText("Ollama"));

    await findByText("Pick a model");
    const select = getByTestId("setup-model") as HTMLSelectElement;
    expect(select.value).toBe("llama3");
    fireEvent.change(select, { target: { value: "mistral" } });
    fireEvent.click(getByText("Next"));

    await findByText("Name your assistant");
    fireEvent.change(getByDisplayValue("Ada"), { target: { value: "Izzy" } });
    fireEvent.click(getByText("Create"));

    await waitFor(() => expect(useApp.getState().botId).toBe("b1"));
    expect(useApp.getState().chatId).toBe("c1");
    expect(useApp.getState().screen).toBe("chat");
    expect(useApp.getState().nudge).toBe("Ask it anything.");

    const calls = mocked.mock.calls.map(([path, options]) => `${options?.method || "GET"} ${path}`);
    const order = ["POST /api/endpoints", "POST /api/bots", "PUT /api/setup", "GET /api/bots/b1/ongoing"];
    const indexes = order.map((call) => calls.indexOf(call));
    expect(indexes.every((index) => index >= 0)).toBe(true);
    expect(indexes).toEqual([...indexes].sort((a, b) => a - b));

    const endpointCall = mocked.mock.calls.find(([path, options]) => path === "/api/endpoints" && options?.method === "POST");
    expect(endpointCall?.[1]?.json).toEqual({ name: "Ollama", base_url: "http://127.0.0.1:11434/v1", api_key: null, model: "mistral" });
    const botCall = mocked.mock.calls.find(([path, options]) => path === "/api/bots" && options?.method === "POST");
    expect((botCall?.[1]?.json as { name: string }).name).toBe("Izzy");
    expect((botCall?.[1]?.json as { endpoint_id: string }).endpoint_id).toBe("e1");
  });

  it("shows a plain error for a dead manual address and does not advance", async () => {
    installApi();
    world.probeOk = false;
    const { findByText, getByPlaceholderText, getByText, queryByText } = renderGate();
    await findByText("No model server answered on this computer.");
    fireEvent.change(getByPlaceholderText("http://localhost:8080/v1"), { target: { value: "http://127.0.0.1:9999/v1" } });
    fireEvent.click(getByText("Check"));
    await findByText("Nothing answered there.");
    expect(queryByText("Pick a model")).toBeNull();
    expect(mocked.mock.calls.some(([path, options]) => path === "/api/endpoints/probe" && options?.method === "POST")).toBe(true);
  });

  it("advances from a manual address that answers the probe", async () => {
    installApi();
    world.probeOk = true;
    const { findByText, getByPlaceholderText, getByText } = renderGate();
    await findByText("No model server answered on this computer.");
    fireEvent.change(getByPlaceholderText("http://localhost:8080/v1"), { target: { value: "http://elsewhere:1234/v1" } });
    fireEvent.click(getByText("Check"));
    await findByText("Pick a model");
    await findByText("remote-model");
  });

  it("Set up later marks the setup flag and closes the wizard", async () => {
    installApi();
    const { findByText, getByTestId, queryByText } = renderGate();
    await findByText("Find your model server");
    fireEvent.click(getByTestId("setup-later"));
    await waitFor(() => expect(world.onboarded).toBe(true));
    await waitFor(() => expect(queryByText("Find your model server")).toBeNull());
    expect(mocked.mock.calls.some(([path, options]) => path === "/api/setup" && options?.method === "PUT")).toBe(true);
  });
});
