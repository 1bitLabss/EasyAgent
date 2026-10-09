import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

test("create a bot, keep a stream going, delete a chat, and remove the bot", async ({ page, request }) => {
  const mock = readFileSync("/tmp/easyagent-e2e/mock.url", "utf8").trim();
  await page.goto("/");
  await expect(page.getByText("AI agents, made easy.").first()).toBeVisible();

  await page.getByRole("button", { name: "You" }).click();
  await page.getByRole("button", { name: "Connections" }).click();
  await page.getByRole("main").getByRole("button", { name: "Add", exact: true }).click();
  await page.getByRole("main").getByRole("textbox", { name: "Name", exact: true }).fill("Mock");
  await page.getByLabel("Address").fill(mock);
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText(mock)).toBeVisible();
  await page.getByRole("button", { name: "Close" }).click();

  await page.getByRole("button", { name: "Add bot" }).first().click();
  await page.locator("form").getByRole("textbox", { name: "Name", exact: true }).fill("Ada");
  await page.locator('select[name="endpoint_id"]').selectOption({ label: "Mock" });
  await page.locator("form").getByRole("button", { name: "Add bot" }).click();
  await expect(page.getByRole("heading", { name: "Ada" })).toBeVisible();

  await page.getByLabel("Message").fill("hello");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Mock says hello.")).toBeVisible();

  await page.getByRole("button", { name: "Add bot" }).first().click();
  await page.locator("form").getByRole("textbox", { name: "Name", exact: true }).fill("Bea");
  await page.locator('select[name="endpoint_id"]').selectOption({ label: "Mock" });
  await page.locator("form").getByRole("button", { name: "Add bot" }).click();
  await expect(page.getByRole("heading", { name: "Bea" })).toBeVisible();

  await page.getByRole("button", { name: "Ada", exact: true }).click();
  await page.getByLabel("Message").fill("take your time");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Here")).toBeVisible();

  await page.keyboard.press("Control+K");
  await page.getByPlaceholder("Bot name").fill("Bea");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Bea" })).toBeVisible();
  await expect(page.getByRole("main")).not.toContainText("today.");

  const bots = await request.get("/api/bots").then((response) => response.json());
  const ada = bots.find((bot: { name: string; id: string }) => bot.name === "Ada");
  await expect.poll(async () => {
    const chats = await request.get(`/api/bots/${ada.id}/chats`).then((response) => response.json());
    const chat = await request.get(`/api/bots/${ada.id}/chats/${chats[0].id}`).then((response) => response.json());
    return (chat.messages || []).map((message: { content?: string }) => message.content || "").join("\n");
  }).toContain("today.");

  await page.getByRole("button", { name: "Ada", exact: true }).click();
  await expect(page.getByText("today.")).toBeVisible();

  await page.getByRole("button", { name: "You" }).click();
  await page.getByRole("button", { name: "This bot's settings" }).click();
  await page.getByRole("button", { name: "Delete chat" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Delete chat" }).click();
  await expect(page.getByText("today.")).toHaveCount(0);
  await expect(page.getByLabel("Message")).toBeVisible();

  await page.getByRole("button", { name: "You" }).click();
  await page.getByRole("button", { name: "This bot's settings" }).click();
  await page.getByRole("button", { name: "Remove bot" }).click();
  await page.getByLabel(/Type Ada to confirm/).fill("  aDa ");
  await page.getByRole("dialog").getByRole("button", { name: "Remove bot" }).click();
  await expect(page.getByRole("button", { name: "Ada", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Bea", exact: true })).toBeVisible();
});
