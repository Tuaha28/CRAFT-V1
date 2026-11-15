import { expect, test, type Page } from "@playwright/test";

/* End-to-end checks of the client app against a running test server.
   Read-only: nothing here requests a plan, decides one or changes a user.
   Sign-in comes from CRAFT_E2E_USER / CRAFT_E2E_PASSWORD so no password lives in the repo. */

const USER = process.env.CRAFT_E2E_USER ?? "";
const PASSWORD = process.env.CRAFT_E2E_PASSWORD ?? "";
// The client sees CRAFT only: no baseline rules or research wording anywhere in the UI.
const BANNED = /\b(FIFO|EDD|SPT|CP-SAT|V3|experiments?|paper)\b/i;
const PAGES = ["/", "/plan", "/runs", "/floor/line", "/floor/board", "/floor/issues", "/floor/quality", "/orders",
  "/styles", "/workforce", "/machines", "/materials", "/quality", "/compare", "/data", "/activity"];

test.describe.configure({ mode: "serial" });
test.skip(!USER || !PASSWORD, "Set CRAFT_E2E_USER and CRAFT_E2E_PASSWORD to run the browser checks");

let page: Page;

const took: Record<string, number> = {};

async function open(path: string) {
  const start = Date.now();
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await expect(page.locator("main .skeleton")).toHaveCount(0, { timeout: 60_000 });
  took[path] = Math.max(took[path] ?? 0, Date.now() - start);
}

test.beforeAll(async ({ browser }) => {
  page = await browser.newPage();
  await page.goto("/");
  await page.getByLabel("Username").fill(USER);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.locator(".stat-tile")).toHaveCount(4, { timeout: 60_000 });
});

test.afterAll(async () => {
  console.log("Slowest pages (ms):", Object.entries(took).sort((a, b) => b[1] - a[1]).slice(0, 5));
  await page.close();
});

test("dashboard opens with the plan health panel and its four figures", async () => {
  await open("/");
  await expect(page.locator(".plan-health")).toBeVisible();
  await expect(page.locator(".stat-tile")).toHaveCount(4);
  await expect(page.locator(".insights li").first()).toBeVisible();
});

test("no baseline or research wording on any page", async () => {
  test.setTimeout(PAGES.length * 60_000);
  for (const path of PAGES) {
    await open(path);
    const text = await page.locator("main").innerText();
    expect(text.match(BANNED)?.[0], `banned word on ${path}`).toBeUndefined();
  }
});

test("orders: a count card filters the table and a picked order opens beside it", async () => {
  await open("/orders");
  await page.locator("button.ring-card", { hasText: "Overdue" }).click();
  await expect(page).toHaveURL(/status=OVERDUE/);
  const rows = page.locator("tr.clickable");
  await expect(rows.first()).toBeVisible();
  // The previous rows stay on screen until the filtered page arrives, so wait for it.
  await expect(rows.locator("td:last-child .chip").filter({ hasNotText: /^Overdue$/ })).toHaveCount(0);
  const chips = await rows.locator("td:last-child .chip").allInnerTexts();
  expect(chips.length).toBeGreaterThan(0);

  const second = rows.nth(Math.min(1, chips.length - 1));
  const po = (await second.locator("td").first().locator("strong").innerText()).trim();
  await second.click();
  await expect(page.locator(".order-card h2")).toHaveText(po);
  await expect(second).toHaveClass(/picked/);

  // Pressing the same card again shows every order.
  await page.locator("button.ring-card", { hasText: "Overdue" }).click();
  await expect(page).not.toHaveURL(/status=OVERDUE/);
});

test("order progress never counts more pieces than were ordered", async () => {
  // Regression: work and quality records were once summed through one join (2,420 of 240 pieces).
  await open("/orders");
  const text = await page.locator(".order-progress").innerText({ timeout: 60_000 });
  const [done, ordered] = (text.match(/([\d,]+) of ([\d,]+)/) ?? []).slice(1).map((n) => Number(n.replace(/,/g, "")));
  expect(ordered).toBeGreaterThan(0);
  expect(done).toBeLessThanOrEqual(ordered);
});

test("theme: the switch changes the page and is remembered", async () => {
  await open("/");
  const theme = () => page.evaluate(() => document.documentElement.dataset.theme);
  const background = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  const before = await theme(), ground = await background();
  await page.getByRole("button", { name: /Switch to the (light|dark) theme/ }).click();
  await expect.poll(theme).not.toBe(before);
  await expect.poll(background).not.toBe(ground);
  await page.reload();
  await expect(page.locator(".stat-tile")).toHaveCount(4, { timeout: 60_000 });
  expect(await theme()).not.toBe(before);
  await page.getByRole("button", { name: /Switch to the (light|dark) theme/ }).click();
  await expect.poll(theme).toBe(before);
});

test("home: deliveries and line timeline, both open an order", async () => {
  await open("/");
  await expect(page.locator("main canvas")).toHaveCount(0);  // the 3D floor lives only in a plan's own tab
  const rows = page.locator(".delivery-list li");
  await expect(rows.first()).toBeVisible({ timeout: 60_000 });
  const late = page.locator(".filter-tabs button", { hasText: "Late" });
  const lateCount = Number(await late.locator(".count-pill").innerText());
  await late.click();
  await expect(rows).toHaveCount(Math.min(lateCount, 5));
  await rows.first().locator("button").click();
  await expect(page.locator("dialog.drawer[open]")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator("dialog.drawer[open]")).toHaveCount(0);
  await page.locator(".tl-bar").first().click();
  await expect(page.locator("dialog.drawer[open]")).toBeVisible();
  await page.keyboard.press("Escape");
});

test("Ask CRAFT answers from the plan and the database, and opens an order", async () => {
  await open("/");
  await page.getByRole("button", { name: "Ask CRAFT", exact: true }).click();
  const panel = page.locator("dialog.ask-drawer[open]");
  await expect(panel).toBeVisible();
  await panel.getByRole("button", { name: "Is any material short?" }).click();
  await expect(panel.locator(".ask-answer .ask-title").last()).toHaveText(/material|Stock covers/, { timeout: 60_000 });
  await panel.getByRole("button", { name: "Which orders are late?" }).click();
  const answer = panel.locator(".ask-answer").last();
  await expect(answer.locator(".ask-title")).toHaveText(/late in this plan|No order is late/);
  expect(await panel.innerText()).not.toMatch(BANNED);
  const order = answer.locator(".ask-items button").first();
  if (await order.count()) {
    await order.click();
    await expect(page.locator("dialog.drawer[open] #drawer-title")).toBeVisible();
    await page.keyboard.press("Escape");
  } else await page.keyboard.press("Escape");
});

test("plan page: figures, schedule and tabs", async () => {
  await open("/runs");
  const finished = page.locator("tbody tr", { hasText: "Finished" }).first();
  test.skip(!(await finished.count()), "No finished plan on this server");
  await finished.locator("a").first().click();
  await expect(page.locator(".plan-cards .plan-card")).toHaveCount(4, { timeout: 60_000 });
  await expect(page.getByRole("heading", { name: "Schedule" })).toBeVisible();
  await page.getByRole("tab", { name: "On the floor" }).click();
  await expect(page.locator("#plan-tab .scene canvas, #plan-tab .factory-map")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByRole("button", { name: /Play the plan|Pause the plan/ })).toBeVisible();
  for (const name of ["Deliveries", "How CRAFT built it", "History"]) {
    await page.getByRole("tab", { name }).click();
    await expect(page.getByRole("tab", { name })).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#plan-tab")).not.toBeEmpty();
  }
});

test("merged pages: one menu entry each, joined by tabs", async () => {
  const nav = page.locator(".rail nav");
  await expect(nav.getByRole("link", { name: "Compare plans" })).toHaveCount(0);
  for (const [entry, tabs] of [["Resources", ["Workers", "Machines", "Materials"]], ["Quality", ["Checks on the floor", "History"]]] as const) {
    await nav.getByRole("link", { name: entry, exact: true }).click();
    for (const tab of tabs) {
      await page.locator(".page-tabs").getByRole("link", { name: tab }).click();
      await expect(page.locator(".page-tabs a.on")).toHaveText(tab);
      await expect(nav.getByRole("link", { name: entry, exact: true })).toHaveClass(/active/);
    }
  }
});

test("phone width: no page scrolls sideways and the menu opens", async () => {
  test.setTimeout(PAGES.length * 60_000);
  await page.setViewportSize({ width: 390, height: 844 });
  try {
    for (const path of PAGES) {
      await open(path);
      const [scroll, width] = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
      expect(scroll, `sideways scroll on ${path}`).toBeLessThanOrEqual(width + 1);
    }
    await page.getByRole("button", { name: "Menu" }).click();
    await expect(page.locator(".rail nav").getByRole("link", { name: "Orders" })).toBeVisible();
    await page.getByRole("button", { name: "Close" }).click();
  } finally {
    await page.setViewportSize({ width: 1440, height: 900 });
  }
});
