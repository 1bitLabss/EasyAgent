import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

test("a failed busy send stays one Not-sent row with Retry", async ({ page, request }) => {
  const mock = readFileSync("/tmp/easyagent-e2e-swallow/mock.url", "utf8").trim();
  const endpoint = await request.post("/api/endpoints", { data: { name: "Mock", base_url: mock } });
  expect(endpoint.ok()).toBeTruthy();
  const saved = await endpoint.json();
  const bot = await request.post("/api/bots", { data: { name: "Ada", endpoint_id: saved.id } });
  expect(bot.ok()).toBeTruthy();
  const created = await bot.json();

  await page.goto("/");
  await page.getByRole("button", { name: "Ada", exact: true }).click();
  await page.getByLabel("Message").fill("take your time");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Here")).toBeVisible();

  await page.route("**/api/bots/**/messages", async (route) => {
    const body = route.request().postData() || "";
    if (body.includes("do not lose this")) {
      await route.abort();
      return;
    }
    await route.continue();
  });
  await page.locator("#draft").fill("do not lose this");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByTestId("retry-unsent")).toBeVisible();
  await expect(page.getByText("Not sent. Retry to send it again.")).toBeVisible();
  for (let n = 0; n < 4; n += 1) {
    await page.waitForTimeout(600);
    await expect(page.getByText("do not lose this", { exact: true })).toHaveCount(1);
  }

  await page.unroute("**/api/bots/**/messages");
  await page.getByTestId("retry-unsent").click();
  await expect(page.getByText("do not lose this", { exact: true })).toHaveCount(1);
  const chats = await request.get(`/api/bots/${created.id}/chats`).then((response) => response.json());
  await expect.poll(async () => {
    const chat = await request.get(`/api/bots/${created.id}/chats/${chats[0].id}`).then((response) => response.json());
    return (chat.messages || []).filter((message: { content?: string }) => message.content === "do not lose this").length;
  }).toBe(1);
});
