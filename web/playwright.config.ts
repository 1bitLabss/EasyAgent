import { defineConfig, devices } from "@playwright/test";

const chrome = { channel: "chrome" as const };

export default defineConfig({
  testDir: "e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  projects: [
    { name: "smoke", testMatch: /smoke.spec.ts/, use: { ...chrome, baseURL: "http://127.0.0.1:44741" } },
    { name: "thinking", testMatch: /thinking.spec.ts/, use: { ...chrome, baseURL: "http://127.0.0.1:44742" } },
    { name: "mobile", testMatch: /mobile.spec.ts/, use: { ...chrome, baseURL: "http://127.0.0.1:44743" } },
    {
      name: "iphone",
      testMatch: /ios.spec.ts/,
      use: { ...devices["iPhone 14"], baseURL: "http://127.0.0.1:44744" },
    },
  ],
  webServer: [
    {
      command: "/usr/bin/python3 e2e/serve.py /tmp/easyagent-e2e 44741",
      url: "http://127.0.0.1:44741/api/health",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: "/usr/bin/python3 e2e/serve.py /tmp/easyagent-e2e-think 44742",
      url: "http://127.0.0.1:44742/api/health",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: "/usr/bin/python3 e2e/serve.py /tmp/easyagent-e2e-phone 44743",
      url: "http://127.0.0.1:44743/api/health",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: "/usr/bin/python3 e2e/serve.py /tmp/easyagent-e2e-ios 44744",
      url: "http://127.0.0.1:44744/api/health",
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
