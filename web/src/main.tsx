import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { queryClient } from "@/api";
import { App } from "@/App";
import "@/index.css";
import "@/face.css";
import "highlight.js/styles/github.css";

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    void registerShell();
  });
}

// Register the service worker under a version-stamped URL so a new app version
// installs a new worker (and its versioned shell cache) instead of reusing the
// old one. The version comes from /api/health, the same one the About screen shows.
async function registerShell() {
  let version = "dev";
  try {
    const health = (await fetch("/api/health", { cache: "no-store" }).then((response) => response.json())) as { version?: string };
    version = String(health?.version || "dev");
  } catch {
    // Server not answering yet; fall back to the dev cache name.
  }
  navigator.serviceWorker.register(`/sw.js?v=${encodeURIComponent(version)}`).catch(() => undefined);
}

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </StrictMode>,
  );
}
