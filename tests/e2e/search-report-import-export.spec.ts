import { readFile } from "node:fs/promises";

import { expect, test, type Page } from "@playwright/test";

import { collectBrowserDiagnostics } from "./helpers";

const password = process.env.E2E_PASSWORD ?? "DemoPass123!";
const baseURL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:8088";

async function loginAs(page: Page, username: string) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.getByLabel(/username|email/i).first().fill(username);
  await page.getByLabel(/password/i).first().fill(password);
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/login/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: /sign in|log in/i }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBeLessThan(300);
  await expect(page.locator("main")).toBeVisible();
  await expect(page).not.toHaveURL(/\/login(?:[/?#]|$)/);
}

async function searchFromHeader(page: Page, query: string) {
  const search = page.locator(".global-search");
  await search.getByLabel("Search assets, work orders, parts, vendors, and components").fill(query);
  await search.getByRole("textbox").press("Enter");
  await expect(page).toHaveURL(`/search?q=${encodeURIComponent(query)}`);
  await expect(page.getByRole("heading", { name: `Results for “${query}”` })).toBeVisible();
}

async function downloadExport(page: Page) {
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export data" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/^fleetline-export-\d{4}-\d{2}-\d{2}\.json$/);
  const path = await download.path();
  expect(path, "completed export download path").not.toBeNull();
  return JSON.parse(await readFile(path!, "utf8")) as Record<string, unknown>;
}

test("global search resolves each supported record type and alternate part numbers", async ({ page }, testInfo) => {
  const diagnostics = collectBrowserDiagnostics(page);
  try {
    await loginAs(page, "purchasing.manager@example.com");

    await searchFromHeader(page, "TRK-012");
    await expect(page.getByRole("link", { name: /TRK-012.*Asset/i })).toBeVisible();

    await searchFromHeader(page, "WO-DEMO-1001");
    await expect(page.getByRole("link", { name: /WO-DEMO-1001.*Work Order/i })).toBeVisible();

    await searchFromHeader(page, "WIX-51734");
    await expect(page.getByRole("link", { name: /FIL-1001.*Part/i })).toBeVisible();

    await searchFromHeader(page, "NAPA Heavy Duty");
    await expect(page.getByRole("link", { name: /NAPA Heavy Duty.*Vendor/i })).toBeVisible();
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});

test("manager report traces a summary row to its source work order", async ({ page }, testInfo) => {
  const diagnostics = collectBrowserDiagnostics(page);
  try {
    await loginAs(page, "fleet.manager@example.com");
    const reportResponse = page.waitForResponse(
      (response) => response.url().endsWith("/api/v1/reports/operations/") && response.request().method() === "GET",
    );
    await page.locator("aside").getByRole("link", { name: "Reports", exact: true }).click();
    expect((await reportResponse).status()).toBe(200);

    await expect(page.getByRole("heading", { name: "Reports", exact: true })).toBeVisible();
    await expect(page.getByRole("region", { name: "Report summary" })).toContainText("Open work orders");
    const source = page.getByRole("link", { name: "WO-DEMO-1001", exact: true });
    await expect(source).toBeVisible();
    await source.click();
    await expect(page).toHaveURL(/\/work-orders\/[0-9a-f-]+$/);
    await expect(page.getByRole("heading", { name: /WO-DEMO-1001 · TRK-012/ })).toBeVisible();
    await expect(page.getByText("Diagnose steering vibration", { exact: true }).first()).toBeVisible();
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});

test("CSV import creates valid assets, clearly reports rejected rows, and records an audit event", async ({ page }, testInfo) => {
  const diagnostics = collectBrowserDiagnostics(page);
  try {
    await loginAs(page, "fleet.manager@example.com");
    await page.locator("aside").getByRole("link", { name: "Reports", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Import assets from CSV" })).toBeVisible();

    await page.getByLabel("CSV file").setInputFiles({
      name: "assets-e2e.csv",
      mimeType: "text/csv",
      buffer: Buffer.from(
        "unit_number,asset_type,location_code,vin\n" +
        "E2E-CSV-401,Truck,MAIN,1FTFW1E50NFA00401\n" +
        ",Truck,MAIN,\n",
      ),
    });
    const importResponsePromise = page.waitForResponse(
      (response) => response.url().endsWith("/api/v1/import/assets/") && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Import assets" }).click();
    const importResponse = await importResponsePromise;
    expect(importResponse.status(), await importResponse.text()).toBe(200);
    expect(await importResponse.json()).toMatchObject({
      created_count: 1,
      rejected_count: 1,
      created: [{ row: 2, unit_number: "E2E-CSV-401" }],
      rejected: [{ row: 3, error: "unit_number is required" }],
    });

    await expect(page.getByText(/Import complete/i)).toBeVisible();
    await expect(page.getByText(/1 created/i)).toBeVisible();
    await expect(page.getByText(/1 rejected/i)).toBeVisible();
    await expect(page.getByText(/Row 3.*unit_number is required/i)).toBeVisible();

    const assetsResponse = await page.request.get("/api/v1/assets/?q=E2E-CSV-401");
    expect(assetsResponse.status(), await assetsResponse.text()).toBe(200);
    const assets = (await assetsResponse.json()) as { assets: Array<{ id: string; unit_number: string }> };
    expect(assets.assets).toHaveLength(1);
    expect(assets.assets[0]).toMatchObject({ unit_number: "E2E-CSV-401" });

    const auditResponse = await page.request.get(
      `/api/v1/audit-events/?resource_type=Asset&resource_id=${assets.assets[0].id}`,
    );
    expect(auditResponse.status(), await auditResponse.text()).toBe(200);
    expect(await auditResponse.json()).toMatchObject({
      events: [{ action: "asset.imported", actor: "fleet.manager@example.com" }],
    });
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});

test("exports are machine-readable and search/export remain permission- and tenant-scoped", async ({ browser }, testInfo) => {
  const managerContext = await browser.newContext({ baseURL });
  const managerPage = await managerContext.newPage();
  const managerDiagnostics = collectBrowserDiagnostics(managerPage);
  const otherContext = await browser.newContext({ baseURL });
  const otherPage = await otherContext.newPage();
  const otherDiagnostics = collectBrowserDiagnostics(otherPage);
  const driverContext = await browser.newContext({ baseURL });
  const driverPage = await driverContext.newPage();
  const driverDiagnostics = collectBrowserDiagnostics(driverPage);
  try {
    await loginAs(managerPage, "fleet.manager@example.com");
    await managerPage.locator("aside").getByRole("link", { name: "Reports", exact: true }).click();
    const managerExport = await downloadExport(managerPage);
    expect(managerExport).toMatchObject({
      schema_version: "1.0",
      organization: { name: "Gator Fleet Services" },
    });
    expect(managerExport.assets).toEqual(
      expect.arrayContaining([expect.objectContaining({ unit_number: "TRK-012" })]),
    );
    expect(managerExport.work_orders).toEqual(
      expect.arrayContaining([expect.objectContaining({ number: "WO-DEMO-1001" })]),
    );
    expect(managerExport.parts).toEqual(
      expect.arrayContaining([expect.objectContaining({ number: "FIL-1001" })]),
    );
    expect(managerExport.vendors).toEqual(
      expect.arrayContaining([expect.objectContaining({ name: "NAPA Heavy Duty" })]),
    );
    expect(JSON.stringify(managerExport)).not.toContain("OTHER-001");
    expect(JSON.stringify(managerExport)).not.toContain("PRIVATE-001");

    await loginAs(otherPage, "other.manager@example.com");
    await searchFromHeader(otherPage, "TRK-012");
    await expect(otherPage.getByText("No matching records found.")).toBeVisible();
    await otherPage.locator("aside").getByRole("link", { name: "Reports", exact: true }).click();
    const otherExport = await downloadExport(otherPage);
    expect(otherExport).toMatchObject({ organization: { name: "Other Fleet Company" } });
    expect(otherExport.assets).toEqual(
      expect.arrayContaining([expect.objectContaining({ unit_number: "OTHER-001" })]),
    );
    expect(JSON.stringify(otherExport)).not.toContain("TRK-012");
    expect(JSON.stringify(otherExport)).not.toContain("FIL-1001");

    await loginAs(driverPage, "driver@example.com");
    await searchFromHeader(driverPage, "WIX-51734");
    await expect(driverPage.getByText("No matching records found.")).toBeVisible();
    await expect(driverPage.locator("aside").getByRole("link", { name: "Reports", exact: true })).toHaveCount(0);
    await driverPage.goto("/reports");
    await expect(driverPage.getByRole("heading", { name: "Access denied" })).toBeVisible();
    await expect(driverPage.getByRole("button", { name: "Export data" })).toHaveCount(0);
    const forbiddenExport = await driverPage.request.get("/api/v1/export/");
    expect(forbiddenExport.status()).toBe(403);

    managerDiagnostics.assertClean();
    otherDiagnostics.assertClean();
    driverDiagnostics.assertClean();
  } finally {
    await Promise.all([
      managerDiagnostics.attach(testInfo),
      otherDiagnostics.attach(testInfo),
      driverDiagnostics.attach(testInfo),
    ]);
    await Promise.all([managerContext.close(), otherContext.close(), driverContext.close()]);
  }
});
