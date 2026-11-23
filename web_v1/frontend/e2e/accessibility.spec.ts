import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

/* Automated accessibility check (WCAG 2.2 AA rules axe can test) on the main pages, dark and light.
   Run against a test server: CRAFT_E2E_USER / CRAFT_E2E_PASSWORD, like client.spec.ts. */
const USER = process.env.CRAFT_E2E_USER ?? "";
const PASSWORD = process.env.CRAFT_E2E_PASSWORD ?? "";
const PAGES = ["/", "/runs", "/plan", "/floor/line", "/floor/board", "/floor/issues", "/floor/quality", "/orders",
  "/styles", "/workforce", "/machines", "/materials", "/quality", "/customers", "/data", "/activity"];
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

test.describe.configure({ mode: "serial" });
test.skip(!USER || !PASSWORD, "Set CRAFT_E2E_USER and CRAFT_E2E_PASSWORD to run the browser checks");

let page: Page;

test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext({ reducedMotion: "reduce" });  // no half-faded content while axe reads colours
  page = await context.newPage();
  await page.goto("/");
  await page.getByLabel("Username").fill(USER);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.locator("main h1").first()).toBeVisible({ timeout: 60_000 });
});

test.afterAll(async () => { await page.close(); });

for (const theme of ["dark", "light"] as const) {
  test(`no accessibility violations on the main pages (${theme})`, async () => {
    test.setTimeout(PAGES.length * 45_000);
    await page.evaluate((t) => localStorage.setItem("craft-theme", t), theme);
    const problems: string[] = [];
    for (const path of PAGES) {
      await page.goto(path);
      await expect(page.locator("main h1").first()).toBeVisible();
      await expect(page.locator("main .skeleton")).toHaveCount(0, { timeout: 60_000 });
      const result = await new AxeBuilder({ page }).withTags(TAGS).analyze();
      for (const v of result.violations) {
        problems.push(`${path} ${v.id} (${v.impact}, ${v.nodes.length}): ${v.nodes.slice(0, 2).map((n) => n.target.join(" ")).join(" | ")}`);
      }
    }
    expect(problems, problems.join("\n")).toEqual([]);
  });
}
