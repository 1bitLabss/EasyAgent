import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

test.setTimeout(180_000);

type Row = { role: string; id: string; text: string };
type Saved = { role?: string; content?: string; kind?: string; about?: string };

async function botId(request: APIRequestContext): Promise<string> {
  const bots = await request.get("/api/bots").then((response) => response.json());
  const bot = (bots as { name: string; id: string }[]).find((item) => item.name === "Ada");
  if (!bot) throw new Error("Ada is missing");
  return bot.id;
}

async function savedChat(request: APIRequestContext, id: string) {
  const chats = await request.get(`/api/bots/${id}/chats`).then((response) => response.json());
  const chatId = (chats as { id: string }[])[0]?.id;
  if (!chatId) throw new Error("no chat");
  return request.get(`/api/bots/${id}/chats/${chatId}`).then((response) => response.json());
}

function savedRows(messages: Saved[]): { role: string; text: string }[] {
  return messages
    .filter((message) => message.about !== "activity" && message.about !== "queue")
    .filter((message) => message.kind !== "status" && message.kind !== "pickup" && message.kind !== "activity")
    .filter((message) => message.role === "user" || (message.content || "").trim())
    .map((message) => ({
      role: message.role || "",
      text: (message.content || "").replace(/\s+/g, " ").trim(),
    }));
}

async function screenRows(page: Page): Promise<Row[]> {
  return page.locator("[data-testid=transcript] .message-row[data-role]").evaluateAll((nodes) =>
    nodes.map((node) => ({
      role: node.getAttribute("data-role") || "",
      id: node.getAttribute("data-message-id") || "",
      text: (node.textContent || "").replace(/\s+/g, " ").trim(),
    })),
  );
}

async function send(page: Page, text: string) {
  const box = page.locator("#draft");
  await box.fill(text);
  await expect(box).toHaveValue(text);
  await page.getByRole("button", { name: "Send", exact: true }).click();
}

async function seePause(page: Page): Promise<boolean> {
  const deadline = Date.now() + 8_000;
  while (Date.now() < deadline) {
    const text = await page.getByTestId("run-state").textContent().catch(() => "");
    if ((text || "").includes("Paused, reading")) return true;
    await page.waitForTimeout(20);
  }
  return false;
}

async function expectOrder(page: Page, request: APIRequestContext, id: string) {
  const chat = await savedChat(request, id);
  const saved = savedRows((chat.messages || []) as Saved[]);
  const shown = await screenRows(page);
  expect(shown.map((row) => row.role)).toEqual(saved.map((row) => row.role));
  saved.forEach((row, index) => {
    expect(shown[index]?.text || "").toContain(row.text);
  });
  const seen = new Set<string>();
  for (const row of shown) {
    if (!row.id) continue;
    expect(seen.has(row.id)).toBe(false);
    seen.add(row.id);
  }
  expect(chat.run?.status).not.toBe("running");
  expect(JSON.stringify(chat.messages || [])).not.toContain("nothing was in flight");
  expect(JSON.stringify(chat.messages || [])).not.toContain("waiting.");
}

test("a message during a reply pauses, stays put, and is answered in order", async ({ page, request }) => {
  const mock = readFileSync("/tmp/easyagent-e2e-busy/mock.url", "utf8").trim();
  const endpoint = await request.post("/api/endpoints", { data: { name: "Mock", base_url: mock } });
  expect(endpoint.ok()).toBeTruthy();
  const saved = await endpoint.json();
  const bot = await request.post("/api/bots", { data: { name: "Ada", endpoint_id: saved.id } });
  expect(bot.ok()).toBeTruthy();
  const id = (await bot.json()).id as string;

  await page.goto("/");
  await page.getByRole("button", { name: "Ada", exact: true }).click();
  await expect(page.getByTestId("run-state")).toHaveText("Idle");

  const trials = [
    { tag: "e1", word: "e1-03", extra: 0, pivot: "pivot-1" },
    { tag: "e2", word: "e2-08", extra: 40, pivot: "pivot-2" },
    { tag: "e3", word: "e3-02", extra: 180, pivot: "pivot-3" },
    { tag: "e4", word: "e4-12", extra: 90, pivot: "pivot-4" },
    { tag: "e5", word: "e5-05", extra: 260, pivot: "pivot-5" },
  ];

  for (const trial of trials) {
    await send(page, `long essay ${trial.tag}`);
    await expect(page.getByText(trial.word, { exact: false }).first()).toBeVisible();
    await expect(page.getByTestId("run-state")).toContainText("Writing");
    if (trial.extra) await page.waitForTimeout(trial.extra);
    const before = await screenRows(page);
    const partialAt = before.findIndex((row) => row.role === "assistant" && row.text.includes(trial.word));
    expect(partialAt).toBeGreaterThanOrEqual(0);
    const partial = before[partialAt]?.text || "";
    const pause = seePause(page);
    await send(page, trial.pivot);
    await expect(page.getByText(trial.pivot, { exact: false }).first()).toBeVisible();
    const mid = await screenRows(page);
    const userAt = mid.filter((row) => row.role === "user" && row.text.includes(trial.pivot));
    expect(userAt).toHaveLength(1);
    const still = mid.findIndex((row) => row.role === "assistant" && row.text.includes(trial.word));
    const asked = mid.findIndex((row) => row.role === "user" && row.text.includes(trial.pivot));
    expect(still).toBeGreaterThanOrEqual(0);
    expect(asked).toBeGreaterThan(still);
    expect(mid[still]?.text || "").toContain(trial.word);
    expect(partial).toContain(trial.word);
    expect(await pause).toBe(true);
    await expect(page.getByText(`Noted ${trial.pivot}.`)).toBeVisible();
    await expect(page.getByTestId("run-state")).toHaveText("Idle");
    await page.locator("#draft").fill("ready");
    await expect(page.getByRole("button", { name: "Send", exact: true })).toBeEnabled();
    await page.locator("#draft").fill("");
    await expect(page.getByText("nothing was in flight")).toHaveCount(0);
    await expect(page.locator("[data-testid=status-note]")).toHaveCount(0);
    const after = await screenRows(page);
    const kept = after.findIndex((row) => row.role === "assistant" && row.text.includes(trial.word));
    const question = after.findIndex((row) => row.role === "user" && row.text.includes(trial.pivot));
    const answer = after.findIndex((row) => row.role === "assistant" && row.text.includes(`Noted ${trial.pivot}.`));
    expect(kept).toBeGreaterThanOrEqual(0);
    expect(question).toBeGreaterThan(kept);
    expect(answer).toBeGreaterThan(question);
    expect(after.filter((row) => row.role === "user" && row.text.includes(trial.pivot))).toHaveLength(1);
    expect(after.filter((row) => row.text.includes(`Noted ${trial.pivot}.`))).toHaveLength(1);
    await expectOrder(page, request, id);
  }

  await send(page, "long essay e9");
  await expect.poll(async () => {
    const chat = await savedChat(request, id);
    return JSON.stringify(chat.messages || []);
  }).toContain("e9-04");
  await page.reload();
  await page.getByRole("button", { name: "Ada", exact: true }).click();
  await expect(page.getByText("e9-04", { exact: false }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeVisible();
  await expect(page.getByTestId("run-state")).not.toHaveText("Idle");
  await expect(page.getByText("e9-08", { exact: false }).first()).toBeVisible();
  await expect(page.getByText("nothing was in flight")).toHaveCount(0);
  await page.getByRole("button", { name: "Stop", exact: true }).click();
  await expect(page.getByTestId("run-state")).toHaveText("Idle");
  await expect(page.getByText("e9-04", { exact: false }).first()).toBeVisible();
  await page.locator("#draft").fill("ready");
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeEnabled();
  await page.locator("#draft").fill("");
  const reloaded = await savedChat(request, id);
  expect(JSON.stringify(reloaded.messages || [])).toContain("e9-04");
  expect(JSON.stringify(reloaded.messages || [])).not.toContain("nothing was in flight");

  await send(page, "long essay e8");
  await expect(page.getByText("e8-03", { exact: false }).first()).toBeVisible();
  const burstPause = seePause(page);
  await send(page, "burst-one");
  await send(page, "burst-two");
  await send(page, "burst-three");
  await expect(page.getByText("burst-one", { exact: false }).first()).toBeVisible();
  await expect(page.getByText("burst-two", { exact: false }).first()).toBeVisible();
  await expect(page.getByText("burst-three", { exact: false }).first()).toBeVisible();
  expect(await burstPause).toBe(true);
  await expect(page.getByText("Answered burst-one, burst-two, and burst-three together.")).toBeVisible();
  await expect(page.getByTestId("run-state")).toHaveText("Idle");
  const burst = await screenRows(page);
  const essay = burst.findIndex((row) => row.role === "assistant" && row.text.includes("e8-03"));
  const one = burst.findIndex((row) => row.role === "user" && row.text.includes("burst-one"));
  const two = burst.findIndex((row) => row.role === "user" && row.text.includes("burst-two"));
  const three = burst.findIndex((row) => row.role === "user" && row.text.includes("burst-three"));
  const together = burst.findIndex((row) => row.text.includes("Answered burst-one, burst-two, and burst-three together."));
  expect(essay).toBeGreaterThanOrEqual(0);
  expect(one).toBeGreaterThan(essay);
  expect(two).toBeGreaterThan(one);
  expect(three).toBeGreaterThan(two);
  expect(together).toBeGreaterThan(three);
  expect(burst.filter((row) => row.role === "user" && row.text.includes("burst-one"))).toHaveLength(1);
  expect(burst.filter((row) => row.role === "user" && row.text.includes("burst-two"))).toHaveLength(1);
  expect(burst.filter((row) => row.role === "user" && row.text.includes("burst-three"))).toHaveLength(1);
  expect(burst.filter((row) => row.text.includes("together."))).toHaveLength(1);
  await expect(page.getByText("nothing was in flight")).toHaveCount(0);
  await expect(page.locator("[data-testid=status-note]")).toHaveCount(0);
  await page.locator("#draft").fill("ready");
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeEnabled();
  await page.locator("#draft").fill("");
  await expectOrder(page, request, id);
  expect(await botId(request)).toBe(id);
});
