import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig(({ command }) => ({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  base: command === "build" ? "/ui/" : "/",
  server: {
    host: "127.0.0.1",
    port: 44731,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:44721",
      "/static": "http://127.0.0.1:44721",
      "/classic": "http://127.0.0.1:44721",
    },
  },
  build: {
    outDir: path.resolve(__dirname, "../easyagent/ui"),
    emptyOutDir: true,
    assetsDir: "assets",
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts"],
  },
}));
