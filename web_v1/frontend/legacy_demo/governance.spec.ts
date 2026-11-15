import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import path from "node:path";

const root = path.resolve(process.cwd(), "../..");

test("manager runs real controlled CRAFT, reviews Gantt, and promotes TEST version", async ({ page }) => {
  const username = process.env.CRAFT_BROWSER_TEST_USER;
  const password = process.env.CRAFT_BROWSER_TEST_PASSWORD;
  const workerPython = process.env.CRAFT_BROWSER_TEST_WORKER_PYTHON;
  if (!username || !password || !workerPython) throw new Error("Isolated browser runner credentials are missing");

  await page.goto("/craft/governance");
  await expect(page.getByText("ISOLATED TEST")).toBeVisible();
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByLabel("Factory test scope")).toBeVisible();
  const simOption = page.getByRole("option").filter({ hasText: "TEST_SIM" });
  const simScopeId = await simOption.getAttribute("value");
  if (!simScopeId) throw new Error("TEST_SIM scope was not advertised");
  await page.getByLabel("Factory test scope").selectOption(simScopeId);
  await page.getByRole("button", { name: "Queue isolated CRAFT proof" }).click();
  await expect(page.getByText("QUEUED", { exact: true })).toBeVisible();

  execFileSync(workerPython, ["web_v1/backend/manage.py", "craft_worker", "--settings=config.settings_integration"],
    { cwd: root, env: process.env, timeout: 180_000, stdio: "pipe" });
  await expect(page.getByText("VALIDATED", { exact: true }).first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("665_2677").first()).toBeVisible();
  await expect(page.getByText("665_2678").first()).toBeVisible();
  await expect(page.getByText("665_2679").first()).toBeVisible();
  await page.getByRole("button", { name: "Compare active and candidate" }).click();
  await expect(page.getByText("SCOPE_VALID")).toBeVisible();
  await expect(page.getByText("PASS", { exact: true }).first()).toBeVisible();

  await page.goto("/schedule");
  await page.getByRole("button", { name: "Validated TEST schedules" }).click();
  await expect(page.getByText(/FACTORY-1 SIMULATED CONTINUATION/i)).toBeVisible();
  await expect(page.getByTestId("controlled-schedule-gantt")).toBeVisible();
  await expect(page.getByText("665_2678").first()).toBeVisible();
  const barEvidence = await page.locator('.wx-task[data-task-id^=":candidate-"]').evaluateAll(elements => elements.map(element => ({
    html: element.outerHTML.slice(0, 500),
    background: getComputedStyle(element).backgroundColor,
    width: element.getBoundingClientRect().width,
    height: element.getBoundingClientRect().height,
  })));
  console.log("CONTROLLED_GANTT_BAR_EVIDENCE", JSON.stringify(barEvidence));
  expect(barEvidence).toHaveLength(3);
  expect(barEvidence.every(bar => bar.width > 0 && bar.height > 0 && bar.background !== "rgba(0, 0, 0, 0)"))
    .toBe(true);
  const artifacts = process.env.CRAFT_BROWSER_ARTIFACT_DIR;
  if (!artifacts) throw new Error("Versioned browser artifact directory is required");
  await page.screenshot({ path: path.join(artifacts, "controlled_gantt.png"), fullPage: true });
  await page.locator('.wx-task[data-task-id=":candidate-665_2677"]').click();
  await expect(page.getByText("Selected validated TEST assignment")).toBeVisible();
  await expect(page.getByText("7369")).toBeVisible();

  await page.goto("/craft/governance");
  await page.getByLabel("Factory test scope").selectOption(simScopeId);
  await page.getByPlaceholder("Required audit reason").fill("Browser-reviewed simulated TEST candidate");
  await page.getByRole("button", { name: "Approve and promote TEST schedule" }).click();
  await expect(page.getByText("ACTIVE", { exact: true }).first()).toBeVisible();
  await page.goto("/schedule");
  await page.getByRole("button", { name: "Validated TEST schedules" }).click();
  await expect(page.getByText("APPROVED TEST").first()).toBeVisible();
});

test("manager urgency changes a real two-order CP-SAT candidate and preserves TEST governance", async ({ page }) => {
  const username = process.env.CRAFT_BROWSER_TEST_USER;
  const password = process.env.CRAFT_BROWSER_TEST_PASSWORD;
  const workerPython = process.env.CRAFT_BROWSER_TEST_WORKER_PYTHON;
  if (!username || !password || !workerPython) throw new Error("Isolated browser runner credentials are missing");
  const work = () => execFileSync(workerPython,
    ["web_v1/backend/manage.py", "craft_worker", "--settings=config.settings_integration"],
    { cwd: root, env: process.env, timeout: 180_000, stdio: "pipe" });

  await page.goto("/craft/governance");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByLabel("Factory test scope")).toBeVisible();
  const priorityOption = page.getByRole("option").filter({ hasText: "TEST_PRIORITY" });
  const priorityScopeId = await priorityOption.getAttribute("value");
  if (!priorityScopeId) throw new Error("TEST_PRIORITY scope was not advertised");
  await page.getByLabel("Factory test scope").selectOption(priorityScopeId);
  await expect(page.getByText("Controlled order priority")).toBeVisible();

  await page.getByRole("button", { name: "Queue isolated CRAFT proof" }).click();
  work();
  await expect(page.getByText("A_finish").first()).toBeVisible({ timeout: 30_000 });
  await page.getByPlaceholder("Required audit reason").fill("Baseline controlled TEST approval");
  await page.getByRole("button", { name: "Approve and promote TEST schedule" }).first().click();
  await expect(page.getByText("ACTIVE", { exact: true }).first()).toBeVisible();

  await page.getByLabel("Priority order").selectOption("B");
  await page.getByLabel("Priority tier").selectOption("URGENT");
  await page.getByLabel("Priority target seconds").fill("900");
  await page.getByLabel("Priority reason").fill("Browser-reviewed synthetic delivery risk");
  await page.getByRole("button", { name: "Record manager priority" }).click();
  await expect(page.getByText(/Current manager priority:.*URGENT/)).toBeVisible();
  const comparisonsBeforeUrgentRun = await page.getByRole("button", { name: "Compare active and candidate" }).count();
  await page.getByRole("button", { name: "Queue isolated CRAFT proof" }).click();
  work();
  await expect(page.getByRole("button", { name: "Compare active and candidate" })).toHaveCount(comparisonsBeforeUrgentRun + 1, { timeout: 30_000 });
  await page.getByRole("button", { name: "Compare active and candidate" }).first().click();
  await expect(page.getByText("Measured order impact")).toBeVisible();
  await expect(page.getByText("3120", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("900", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("SCOPE_VALID").first()).toBeVisible();
  await page.goto("/schedule");
  await page.getByRole("button", { name: "Validated TEST schedules" }).click();
  await expect(page.getByText(/TWO-ORDER SYNTHETIC PRIORITY TEST/i)).toBeVisible();
  await expect(page.getByTestId("controlled-schedule-gantt")).toBeVisible();
  await page.goto("/craft/governance");
  await page.getByLabel("Factory test scope").selectOption(priorityScopeId);
  await page.getByPlaceholder("Required audit reason").fill("Reviewed measured urgent-order tradeoff");
  await page.getByRole("button", { name: "Approve and promote TEST schedule" }).first().click();
  await expect(page.getByText("ORDER_PRIORITY_CHANGED")).toBeVisible();
  await expect(page.getByText("ACTIVE", { exact: true }).first()).toBeVisible();
});
