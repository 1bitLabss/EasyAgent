import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

const shots = "/opt/cursor/artifacts";

test("live reasoning stays visible, then collapses, in light and dark", async ({ page }) => {
  await page.setViewportSize({ width: 1100, height: 820 });
  const mock = readFileSync("/tmp/easyagent-e2e-think/mock.url", "utf8").trim();
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Add a bot." })).toBeVisible();

  await page.getByRole("button", { name: "You" }).click();
  await page.getByRole("button", { name: "Connections" }).click();
  await page.getByRole("main").getByRole("button", { name: "Add", exact: true }).click();
  await page.getByRole("main").getByRole("textbox", { name: "Name", exact: true }).fill("Mock");
  await page.getByLabel("Address").fill(mock);
  await page.getByRole("button", { name: "Save" }).click();
  await page.getByRole("button", { name: "Close" }).click();

  await page.getByRole("button", { name: "Add bot" }).first().click();
  await page.locator("form").getByRole("textbox", { name: "Name", exact: true }).fill("Ada");
  await page.locator('select[name="endpoint_id"]').selectOption({ label: "Mock" });
  await page.locator("form").getByRole("button", { name: "Add bot" }).click();
  await expect(page.getByRole("heading", { name: "Ada" })).toBeVisible();

  await page.getByLabel("Message").fill("show your thinking");
  await page.getByRole("button", { name: "Send" }).click();
  const live = page.getByTestId("thinking-panel").last();
  await expect(live.getByTestId("thinking-body")).toContainText("Looking at the question.");
  await expect(live).toContainText("Thought for");
  await page.screenshot({ path: `${shots}/thinking_panel_live_light.png` });
  await expect(page.getByText("The answer is ready.")).toBeVisible();
  await expect(live.getByTestId("thinking-body")).toHaveCount(0);
  await expect(live).toContainText(/Thinking · Thought for/);
  await page.screenshot({ path: `${shots}/thinking_panel_collapsed_light.png` });

  await page.getByRole("button", { name: "You" }).click();
  await page.getByRole("button", { name: "Dark" }).click();
  await page.getByLabel("Message").fill("show your thinking");
  await page.getByRole("button", { name: "Send" }).click();
  const again = page.getByTestId("thinking-panel").last();
  await expect(again.getByTestId("thinking-body")).toContainText("The answer is a short one.");
  await page.screenshot({ path: `${shots}/thinking_panel_live_dark.png` });
  await expect(page.getByText("The answer is ready.").last()).toBeVisible();
  await expect(again.getByTestId("thinking-body")).toHaveCount(0);
  await page.screenshot({ path: `${shots}/thinking_panel_collapsed_dark.png` });

  await page.getByLabel("Message").fill("think in tags");
  await page.getByRole("button", { name: "Send" }).click();
  const tagged = page.getByTestId("thinking-panel").last();
  await expect(tagged.getByTestId("thinking-body")).toContainText("tag plan from the tags");
  await expect(page.getByText("The tagged answer is ready.")).toBeVisible();
  await expect(tagged.getByTestId("thinking-body")).toHaveCount(0);
  await tagged.getByRole("button").click();
  await expect(tagged.getByTestId("thinking-body")).toContainText("tag plan from the tags");
});
