import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  use: {
    baseURL: "http://127.0.0.1:44741",
    channel: "chrome",
  },
  webServer: {
    command: "/usr/bin/python3 e2e/serve.py /tmp/easyagent-e2e 44741",
    url: "http://127.0.0.1:44741/api/health",
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
