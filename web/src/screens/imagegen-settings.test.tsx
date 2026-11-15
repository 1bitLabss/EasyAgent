import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, queryClient } from "@/api";
import { ImageGenBlock } from "./Settings";

// The saved settings as GET /api/imagegen answers them: the key is never echoed,
// only has_key says one is there.
const saved = { enabled: true, base_url: "http://127.0.0.1:8000/v1", model: "FastSDXL", size: "512x512", has_key: true };

vi.mock("@/api", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, networkMode: "always" } } });
  return {
    queryClient: client,
    api: vi.fn(async () => ({ ...saved })),
  };
});

const mocked = vi.mocked(api);

afterEach(() => {
  cleanup();
  mocked.mockReset();
  queryClient.clear();
});

function paint() {
  render(
    <QueryClientProvider client={queryClient}>
      <ImageGenBlock />
    </QueryClientProvider>,
  );
}

async function painted() {
  await waitFor(() =>
    expect((screen.getByTestId("imagegen-base-url") as HTMLInputElement).value).toBe("http://127.0.0.1:8000/v1"),
  );
}

describe("image generation settings", () => {
  it("shows the saved settings and says a key is already saved", async () => {
    paint();
    await painted();
    expect((screen.getByTestId("imagegen-model") as HTMLInputElement).value).toBe("FastSDXL");
    expect((screen.getByTestId("imagegen-size") as HTMLInputElement).value).toBe("512x512");
    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
    expect((screen.getByTestId("imagegen-key") as HTMLInputElement).placeholder).toBe("Saved — type to replace");
  });

  it("saves the form without a key so the saved key stays", async () => {
    paint();
    await painted();
    fireEvent.click(screen.getByTestId("imagegen-save"));
    await waitFor(() =>
      expect(mocked).toHaveBeenCalledWith(
        "/api/imagegen",
        expect.objectContaining({ method: "PUT", json: { enabled: true, base_url: "http://127.0.0.1:8000/v1", model: "FastSDXL", size: "512x512" } }),
      ),
    );
    const put = mocked.mock.calls.find(([, options]) => options?.method === "PUT");
    expect(put?.[1]?.json).not.toHaveProperty("api_key");
  });

  it("sends a typed key so it replaces the saved one", async () => {
    paint();
    await painted();
    fireEvent.change(screen.getByTestId("imagegen-key"), { target: { value: "sk-new" } });
    fireEvent.click(screen.getByTestId("imagegen-save"));
    await waitFor(() =>
      expect(mocked.mock.calls.some(([, options]) => options?.method === "PUT" && (options.json as { api_key?: string }).api_key === "sk-new")).toBe(true),
    );
  });

  it("shows the server's plain words when the address is refused", async () => {
    paint();
    await painted();
    mocked.mockImplementation(async () => {
      throw new Error("The image address must start with http:// or https://.");
    });
    fireEvent.click(screen.getByTestId("imagegen-save"));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toBe("The image address must start with http:// or https://."),
    );
  });
});
