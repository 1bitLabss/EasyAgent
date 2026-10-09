import { devices, expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

const shots = "/opt/cursor/artifacts";

test.describe.configure({ mode: "serial" });

for (const name of ["iPhone 14", "Pixel 7"] as const) {
  const slug = name === "iPhone 14" ? "iphone14" : "pixel7";

  test(`chat, thinking, and pairing on ${name}`, async ({ browser }) => {
    const context = await browser.newContext({
      ...devices[name],
      baseURL: "http://127.0.0.1:44743",
    });
    const page = await context.newPage();
    const mock = readFileSync("/tmp/easyagent-e2e-phone/mock.url", "utf8").trim();
    await page.goto("/");
    await expect(page.getByText("AI agents, made easy.").first()).toBeVisible();

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

    const rail = await page.locator(".rail").boundingBox();
    const composer = await page.locator(".composer").boundingBox();
    expect(rail && composer && rail.y > composer.y).toBeTruthy();
    const send = await page.getByRole("button", { name: "Send" }).boundingBox();
    expect(send && send.height >= 44 && send.width >= 44).toBeTruthy();

    await page.getByLabel("Message").fill("show your thinking");
    await page.getByRole("button", { name: "Send" }).click();
    const live = page.getByTestId("thinking-panel").last();
    await expect(live.getByTestId("thinking-body")).toContainText("Looking at the question.");
    const cap = await live.getByTestId("thinking-body").evaluate((el) => getComputedStyle(el).maxHeight);
    expect(Number.parseFloat(cap)).toBeLessThanOrEqual(200);
    await live.scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${shots}/phone_chat_${slug}.png` });
    await live.screenshot({ path: `${shots}/phone_thinking_${slug}.png` });
    await expect(page.getByText("The answer is ready.")).toBeVisible();

    await page.getByRole("button", { name: "You" }).click();
    await page.getByRole("button", { name: "Phone", exact: true }).click();
    await page.getByRole("switch", { name: "Phone access" }).click();
    const qr = page.getByTestId("phone-qr");
    await expect(qr).toBeVisible();
    await page.screenshot({ path: `${shots}/phone_pair_${slug}.png` });
    await context.close();
  });
}
