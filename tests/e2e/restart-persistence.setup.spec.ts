import { writeFile } from "node:fs/promises";

import { expect, test } from "@playwright/test";

import { collectBrowserDiagnostics, loginThroughUi } from "./helpers";

const stateFile = process.env.E2E_RESTART_STATE_FILE;
const runId = process.env.E2E_RUN_ID;

test("@restart-persistence creates a durable asset through the UI", async ({ page }, testInfo) => {
  expect(stateFile, "E2E_RESTART_STATE_FILE is required").toBeTruthy();
  expect(runId, "E2E_RUN_ID is required").toBeTruthy();
  const diagnostics = collectBrowserDiagnostics(page);
  try {
    const unitNumber = `RST-${runId}`.toUpperCase();
    await loginThroughUi(page);
    await page.getByRole("link", { name: "Assets", exact: true }).first().click();
    await expect(page.getByRole("heading", { name: "Assets", exact: true })).toBeVisible();
    await page.getByText("New asset", { exact: true }).click();
    await page.getByLabel(/^Unit number/).fill(unitNumber);

    const responsePromise = page.waitForResponse(
      (response) => response.url().endsWith("/api/v1/assets/") && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Create asset", exact: true }).click();
    const response = await responsePromise;
    const payload = (await response.json()) as { asset?: { id?: string; unit_number?: string } };
    expect(response.status(), JSON.stringify(payload)).toBe(201);
    expect(payload.asset?.id).toBeTruthy();
    expect(payload.asset?.unit_number).toBe(unitNumber);
    await expect(page.getByRole("link", { name: unitNumber, exact: true })).toBeVisible();
    await writeFile(stateFile!, JSON.stringify({ id: payload.asset!.id, unitNumber }), "utf8");
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});
