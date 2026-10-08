import { QueryClient } from "@tanstack/react-query";
import { useApp } from "@/store";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false, staleTime: 1500, refetchOnWindowFocus: true },
  },
});

export class ApiError extends Error {
  status: number;
  offline: boolean;
  constructor(message: string, status: number, offline = false) {
    super(message);
    this.status = status;
    this.offline = offline;
  }
}

export function sharedToken(): string {
  try {
    return sessionStorage.getItem("easyagent.token") || "";
  } catch {
    return "";
  }
}

export function saveToken(token: string) {
  sessionStorage.setItem("easyagent.token", token);
}

function headers(extra?: HeadersInit, json = false): Headers {
  const result = new Headers(extra);
  if (json) result.set("Content-Type", "application/json");
  const token = sharedToken();
  if (token) result.set("X-EasyAgent-Token", token);
  return result;
}

export async function api<T>(path: string, options: { method?: string; json?: unknown; body?: BodyInit; headers?: HeadersInit } = {}): Promise<T> {
  const response = await fetch(path, {
    method: options.method || (options.json !== undefined ? "POST" : "GET"),
    headers: headers(options.headers, options.json !== undefined),
    body: options.json !== undefined ? JSON.stringify(options.json) : options.body,
  });
  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text };
    }
  }
  if (!response.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : "";
    const message = typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : response.statusText;
    const offline = response.status === 503 && Boolean(data && typeof data === "object" && (data as { offline?: boolean }).offline);
    if (response.status === 401 && message.toLowerCase().includes("token")) useApp.getState().setTokenNeeded(true);
    if (offline) useApp.getState().setOffline(message);
    throw new ApiError(message || "Request failed", response.status, offline);
  }
  useApp.getState().setOffline("");
  return data as T;
}

export async function streamPost(path: string, json: unknown, signal: AbortSignal): Promise<Response> {
  const response = await fetch(path, {
    method: "POST",
    headers: headers({ Accept: "text/event-stream" }, true),
    body: JSON.stringify(json ?? {}),
    signal,
  });
  if (!response.ok) {
    let message = response.statusText;
    try {
      const data = await response.json();
      if (data && typeof data.detail === "string") message = data.detail;
    } catch {
      /* the status line is enough */
    }
    throw new ApiError(message || "The reply failed.", response.status);
  }
  return response;
}
