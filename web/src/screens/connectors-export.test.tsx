import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, queryClient } from "@/api";
import { ConnectorsBlock } from "./Settings";

const connectorRow = {
  id: "c1",
  name: "Notes",
  transport: "stdio",
  command: ["python", "-m", "easyagent.mcpstd"],
  url: "",
  enabled: true,
  secret_names: [],
  tools: [],
  starter: "filesystem",
  package: "",
  version: "1.0.0",
};

const exported = {
  $schema: "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  mcpServers: { Notes: { type: "stdio", command: "python", args: ["-m", "easyagent.mcpstd"], env: {} } },
};

vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, networkMode: "always" } } });
  return {
    queryClient: client,
    api: vi.fn(async () => []),
  };
});

const mocked = vi.mocked(api);

let createObjectURL: ReturnType<typeof vi.fn>;
let revokeObjectURL: ReturnType<typeof vi.fn>;
let anchorClick: ReturnType<typeof vi.spyOn>;

afterEach(() => {
  cleanup();
  mocked.mockReset();
  queryClient.clear();
  createObjectURL?.mockRestore?.();
  revokeObjectURL?.mockRestore?.();
  anchorClick?.mockRestore?.();
});

function stubDownload() {
  createObjectURL = vi.fn(() => "blob:mcp");
  revokeObjectURL = vi.fn();
  Object.defineProperty(URL, "createObjectURL", { value: createObjectURL, configurable: true });
  Object.defineProperty(URL, "revokeObjectURL", { value: revokeObjectURL, configurable: true });
  anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
}

function paint() {
  render(
    <QueryClientProvider client={queryClient}>
      <ConnectorsBlock botId="bot-1" onError={() => {}} />
    </QueryClientProvider>,
  );
}

describe("connector mcp.json export", () => {
  it("shows the Export button when the bot has connectors", async () => {
    mocked.mockImplementation(async (path: string) => (String(path).endsWith("/export") ? exported : [connectorRow]));
    stubDownload();
    paint();
    await waitFor(() => expect(screen.getByTestId("connectors-export")).toBeTruthy());
  });

  it("shows no Export button when there are no connectors", async () => {
    mocked.mockImplementation(async () => []);
    stubDownload();
    paint();
    await waitFor(() => expect(screen.getByText("Connectors")).toBeTruthy());
    expect(screen.queryByTestId("connectors-export")).toBeNull();
  });

  it("fetches the export and downloads it as mcp.json", async () => {
    mocked.mockImplementation(async (path: string) => (String(path).endsWith("/export") ? exported : [connectorRow]));
    stubDownload();
    paint();
    await waitFor(() => expect(screen.getByTestId("connectors-export")).toBeTruthy());
    fireEvent.click(screen.getByTestId("connectors-export"));
    await waitFor(() =>
      expect(mocked).toHaveBeenCalledWith("/api/bots/bot-1/connectors/export"),
    );
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    expect(anchorClick).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:mcp");
  });
});
