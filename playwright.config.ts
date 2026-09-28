import path from "node:path";

import { defineConfig, devices } from "@playwright/test";

const artifactDir = path.resolve(
  process.env.PLAYWRIGHT_ARTIFACT_DIR ?? "artifacts/e2e/manual",
);

export default defineConfig({
  testDir: "tests/e2e",
  outputDir: path.join(artifactDir, "test-results"),
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: Number(process.env.E2E_WORKERS ?? "1"),
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: [
    ["list"],
    ["html", { outputFolder: path.join(artifactDir, "playwright-report"), open: "never" }],
    ["json", { outputFile: path.join(artifactDir, "results.json") }],
  ],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:8088",
    actionTimeout: 10_000,
    navigationTimeout: 20_000,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "retain-on-failure",
    serviceWorkers: "allow",
  },
  projects: [
    {
      name: "chrome",
      use: { ...devices["Desktop Chrome"], channel: "chrome" },
    },
  ],
});
