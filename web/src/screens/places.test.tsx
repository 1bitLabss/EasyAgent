import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, queryClient } from "@/api";
import { useApp } from "@/store";
import { ConnectionsScreen, RoomsScreen } from "./Places";

vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, networkMode: "always" } } });
  return {
    queryClient: client,
    api: vi.fn(async (path: string, options?: { method?: string }) => {
      if (path === "/api/endpoints") {
        return [{ id: "e1", name: "Home server", base_url: "http://localhost:8080/v1", has_api_key: false, model: "m", max_parallel: 1, max_tokens: 4096, stall_seconds: 45, first_token_seconds: 600 }];
      }
      if (path === "/api/endpoints/e1/test" && options?.method === "POST") {
        return { ok: true, detail: "The server answered." };
      }
      if (path === "/api/rooms") {
        return [{ id: "r1", name: "Desk" }];
      }
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

describe("Connections list", () => {
  it("shows an explicit Edit button that opens the saved connection in the form", async () => {
    const { findByText, getByText, getByDisplayValue } = render(
      <QueryClientProvider client={queryClient}>
        <ConnectionsScreen />
      </QueryClientProvider>,
    );
    await findByText("Home server");
    fireEvent.click(getByText("Edit"));
    await waitFor(() => expect(getByDisplayValue("Home server")).toBeTruthy());
    expect(getByDisplayValue("http://localhost:8080/v1")).toBeTruthy();
  });

  it("keeps Remove beside Edit", async () => {
    const { findByText, getByText } = render(
      <QueryClientProvider client={queryClient}>
        <ConnectionsScreen />
      </QueryClientProvider>,
    );
    await findByText("Home server");
    expect(getByText("Remove")).toBeTruthy();
    expect(getByText("Edit")).toBeTruthy();
  });

  it("tests a connection and shows the answer without sending a message", async () => {
    const { findByText, getByText } = render(
      <QueryClientProvider client={queryClient}>
        <ConnectionsScreen />
      </QueryClientProvider>,
    );
    await findByText("Home server");
    fireEvent.click(getByText("Test"));
    await waitFor(() => expect(mocked.mock.calls.some(([path, options]) => path === "/api/endpoints/e1/test" && options?.method === "POST")).toBe(true));
    await findByText("The server answered.");
  });

  it("offers a vision switch on the connection form", async () => {
    const { findByText, getByText } = render(
      <QueryClientProvider client={queryClient}>
        <ConnectionsScreen />
      </QueryClientProvider>,
    );
    await findByText("Home server");
    fireEvent.click(getByText("Edit"));
    await findByText("This model can see pictures");
  });
});

describe("Rooms list", () => {
  afterEach(() => {
    useApp.setState({ roomId: null, screen: "chat" });
  });

  it("opens a room in the live room pane instead of a blocking page", async () => {
    useApp.setState({ roomId: null, screen: "rooms" });
    const { findByText, getByText } = render(
      <QueryClientProvider client={queryClient}>
        <RoomsScreen />
      </QueryClientProvider>,
    );
    await findByText("Desk");
    fireEvent.click(getByText("Open"));
    expect(useApp.getState().screen).toBe("room");
    expect(useApp.getState().roomId).toBe("r1");
  });
});
