import { devices, expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

test("iPhone Safari adds to the home screen, keeps the composer above the keyboard, and pairs from the Camera app", async ({ page, browser, request }) => {
  await page.goto("/");
  await expect(page.locator('meta[name="apple-mobile-web-app-capable"]')).toHaveAttribute("content", "yes");
  await expect(page.locator('meta[name="mobile-web-app-capable"]')).toHaveAttribute("content", "yes");
  await expect(page.locator('meta[name="apple-mobile-web-app-status-bar-style"]')).toHaveAttribute("content", "black-translucent");
  await expect(page.locator('meta[name="viewport"]')).toHaveAttribute("content", /viewport-fit=cover/);
  await expect(page.locator('link[rel="apple-touch-icon"]')).toHaveAttribute("href", "/static/icons/apple-touch-icon.png");
  const icon = await request.get("/static/icons/apple-touch-icon.png");
  expect(icon.ok()).toBeTruthy();
  expect(icon.headers()["content-type"]).toContain("image/png");
  const manifest = await (await request.get("/manifest.webmanifest")).json();
  expect(manifest.display).toBe("standalone");
  expect(manifest.icons[0].sizes).toBe("180x180");

  const cssHref = await page.locator('link[rel="stylesheet"]').getAttribute("href");
  const css = await (await request.get(cssHref || "")).text();
  expect(css).toContain("safe-area-inset-top");
  expect(css).toContain("safe-area-inset-bottom");
  expect(css).toContain("safe-area-inset-left");
  expect(css.includes("-webkit-overflow-scrolling:touch") || css.includes("-webkit-overflow-scrolling: touch")).toBeTruthy();
  expect(css).toContain("--vv-offset");

  const mock = readFileSync("/tmp/easyagent-e2e-ios/mock.url", "utf8").trim();
  await page.getByRole("button", { name: "You" }).click();
  await page.getByRole("button", { name: "Connections" }).click();
  const name = page.getByRole("main").getByRole("textbox", { name: "Name", exact: true });
  await page.getByRole("main").getByRole("button", { name: "Add", exact: true }).click();
  expect(await name.evaluate((el) => Number.parseFloat(getComputedStyle(el).fontSize))).toBeGreaterThanOrEqual(16);
  await name.fill("Mock");
  await page.getByLabel("Address").fill(mock);
  await page.getByRole("button", { name: "Save" }).click();
  await page.getByRole("button", { name: "Close" }).click();

  await page.getByRole("button", { name: "Add bot" }).first().click();
  await page.locator("form").getByRole("textbox", { name: "Name", exact: true }).fill("Ada");
  await page.locator('select[name="endpoint_id"]').selectOption({ label: "Mock" });
  await page.locator("form").getByRole("button", { name: "Add bot" }).click();
  await expect(page.getByRole("heading", { name: "Ada" })).toBeVisible();

  const message = page.getByLabel("Message");
  expect(await message.evaluate((el) => Number.parseFloat(getComputedStyle(el).fontSize))).toBeGreaterThanOrEqual(16);

  const shifted = await page.evaluate(() => {
    const apply = (window as Window & { __easyagentApplyViewport?: (vv: { height: number; offsetTop: number }) => void }).__easyagentApplyViewport;
    if (!apply) return null;
    apply({ height: 420, offsetTop: 70 });
    const frame = document.querySelector(".app-frame")?.getBoundingClientRect();
    const composer = document.querySelector(".composer")?.getBoundingClientRect();
    return frame && composer
      ? { top: frame.y, height: frame.height, composerBottom: composer.y + composer.height, frameBottom: frame.y + frame.height }
      : null;
  });
  expect(shifted).not.toBeNull();
  expect(shifted!.top).toBeGreaterThanOrEqual(60);
  expect(shifted!.height).toBeLessThanOrEqual(430);
  expect(shifted!.composerBottom).toBeLessThanOrEqual(shifted!.frameBottom + 2);
  await page.evaluate(() => {
    const apply = (window as Window & { __easyagentApplyViewport?: (vv: { height: number; offsetTop: number }) => void }).__easyagentApplyViewport;
    const vv = window.visualViewport;
    apply?.({ height: vv?.height || window.innerHeight, offsetTop: 0 });
  });

  await message.fill("show your thinking");
  await page.getByRole("button", { name: "Send" }).click();
  const live = page.getByTestId("thinking-panel").last();
  await expect(live.getByTestId("thinking-body")).toContainText("Looking at the question.");
  const scrolling = await live.getByTestId("thinking-body").evaluate((el) => {
    const style = getComputedStyle(el);
    return { overflowY: style.overflowY, overscroll: style.overscrollBehavior, touch: style.touchAction };
  });
  expect(["auto", "scroll"]).toContain(scrolling.overflowY);
  expect(scrolling.overscroll).toBe("contain");
  expect(scrolling.touch).toContain("pan-y");
  await expect(page.getByText("The answer is ready.")).toBeVisible();

  await page.getByRole("button", { name: "You" }).click();
  await page.getByRole("button", { name: "Phone", exact: true }).click();
  const toggle = page.getByRole("switch", { name: "Phone access" });
  if (!(await toggle.isChecked())) await toggle.click();
  const pairUrl = (await page.getByTestId("pair-url").innerText()).trim();
  expect(pairUrl).toMatch(/^http:\/\/[^ ]+:\d+\/\?pair=[A-Za-z0-9_-]+$/);

  const camera = await browser.newContext({ ...devices["iPhone 14"] });
  const cameraPage = await camera.newPage();
  await cameraPage.goto(pairUrl);
  await expect(cameraPage.getByLabel("Message")).toBeVisible();
  await expect(cameraPage.getByText("Type the shared token.")).toHaveCount(0);
  const stored = await cameraPage.evaluate(() => localStorage.getItem("easyagent.token"));
  expect(stored && pairUrl.includes(stored)).toBeTruthy();
  expect(cameraPage.url()).toContain("pair=");
  const home = await cameraPage.locator('link[rel="manifest"]').getAttribute("href");
  expect(home).toContain("pair=");
  await camera.close();
});
