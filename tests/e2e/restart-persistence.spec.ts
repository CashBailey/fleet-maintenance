import { readFile } from "node:fs/promises";

import { expect, test } from "@playwright/test";

import { collectBrowserDiagnostics, loginThroughUi } from "./helpers";

interface RestartState {
  id: string;
  unitNumber: string;
}

test("@restart-persistence retains the UI and API record after process restart", async ({ page }, testInfo) => {
  const stateFile = process.env.E2E_RESTART_STATE_FILE;
  expect(stateFile, "E2E_RESTART_STATE_FILE is required").toBeTruthy();
  const state = JSON.parse(await readFile(stateFile!, "utf8")) as RestartState;
  expect(state.id).toMatch(/^[0-9a-f-]{36}$/i);
  expect(state.unitNumber).toMatch(/^RST-/);

  const diagnostics = collectBrowserDiagnostics(page);
  try {
    await loginThroughUi(page);
    await page.getByRole("link", { name: "Assets", exact: true }).first().click();
    await expect(page.getByRole("link", { name: state.unitNumber, exact: true })).toBeVisible();

    const response = await page.context().request.get(`/api/v1/assets/${state.id}/`);
    expect(response.status()).toBe(200);
    expect(await response.json()).toMatchObject({
      asset: { id: state.id, unit_number: state.unitNumber },
    });

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.getByRole("link", { name: state.unitNumber, exact: true })).toBeVisible();
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});
