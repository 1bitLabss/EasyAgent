import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

const MANY_LINES = "line\n".repeat(2000);
const ONE_LINE = "x".repeat(10_000);

async function paste(page: import("@playwright/test").Page, text: string) {
  const box = page.locator("#draft");
  await box.click();
  await page.evaluate(async (value) => {
    await navigator.clipboard.writeText(value);
  }, text);
  await page.keyboard.press("Control+V");
  await expect(box).toHaveValue(text);
}

async function metrics(page: import("@playwright/test").Page) {
  return page.locator("#draft").evaluate((el) => ({
    height: el.getBoundingClientRect().height,
    scrollHeight: el.scrollHeight,
    clientHeight: el.clientHeight,
    scrollWidth: el.scrollWidth,
    clientWidth: el.clientWidth,
  }));
}

test("a 10k paste stays within eight lines, then the box shrinks again", async ({ page, request, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const mock = readFileSync("/tmp/easyagent-e2e-composer/mock.url", "utf8").trim();
  const endpoint = await request.post("/api/endpoints", { data: { name: "Mock", base_url: mock } });
  expect(endpoint.ok()).toBeTruthy();
  const saved = await endpoint.json();
  const bot = await request.post("/api/bots", { data: { name: "Ada", endpoint_id: saved.id } });
  expect(bot.ok()).toBeTruthy();

  await page.goto("/");
  await page.getByRole("button", { name: "Ada", exact: true }).click();
  await expect(page.getByTestId("run-state")).toHaveText("Idle");

  expect(MANY_LINES.length).toBeGreaterThanOrEqual(10_000);
  expect(ONE_LINE.length).toBe(10_000);

  await paste(page, MANY_LINES);
  const wrapped = await metrics(page);
  expect(wrapped.height).toBeLessThanOrEqual(230);
  expect(wrapped.height).toBeGreaterThan(70);
  expect(wrapped.scrollHeight).toBeGreaterThan(wrapped.clientHeight + 8);

  await page.locator("#draft").fill("");
  const cleared = await metrics(page);
  expect(cleared.height).toBeLessThan(72);

  await paste(page, ONE_LINE);
  const single = await metrics(page);
  expect(single.height).toBeLessThanOrEqual(230);
  expect(single.height).toBeGreaterThan(70);
  expect(single.scrollHeight).toBeGreaterThan(single.clientHeight + 8);
  expect(single.scrollWidth).toBeLessThanOrEqual(single.clientWidth + 8);

  await page.locator("#draft").fill("");
  await page.locator("#draft").fill("take your time");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByTestId("run-state")).toContainText("Waiting on model");
  await expect(page.getByTestId("run-state")).toContainText("s");
  await expect(page.getByText("Here")).toBeVisible();
});
