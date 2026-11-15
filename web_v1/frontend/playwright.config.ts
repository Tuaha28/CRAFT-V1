import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 180_000,
  expect: { timeout: 20_000 },
  workers: 1,
  retries: 0,
  outputDir: process.env.CRAFT_BROWSER_ARTIFACT_DIR ? `${process.env.CRAFT_BROWSER_ARTIFACT_DIR}/playwright` : "test-results",
  reporter: "list",
  use: {
    baseURL: process.env.CRAFT_E2E_BASE ?? "http://localhost:5173",
    browserName: "chromium",
    channel: "msedge",
    headless: true,
    viewport: { width: 1440, height: 900 },
  },
});
