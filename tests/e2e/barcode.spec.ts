import { randomUUID } from "node:crypto";

import { expect, test, type BrowserContext, type Page } from "@playwright/test";

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
  expect(response.status(), await response.text()).toBe(200);
  await expect(page.locator("main")).toBeVisible();
}

async function csrfToken(context: BrowserContext) {
  const response = await context.request.get("/api/v1/auth/csrf/");
  expect(response.status(), await response.text()).toBe(200);
  return String((await response.json()).csrf_token);
}

async function createAmbiguousParts(context: BrowserContext) {
  const suffix = randomUUID().slice(0, 8).toUpperCase();
  const sharedIdentifier = `E2E-SHARED-${suffix}`;
  const csrf = await csrfToken(context);
  const parts: Array<{ id: string; number: string }> = [];
  for (const marker of ["A", "B"]) {
    const number = `E2E-BAR-${marker}-${suffix}`;
    const response = await context.request.post("/api/v1/inventory/parts/", {
      headers: { "X-CSRFToken": csrf, "Idempotency-Key": randomUUID() },
      data: {
        number,
        name: `Ambiguous barcode fixture ${marker}`,
        manufacturer_number: sharedIdentifier,
      },
    });
    expect(response.status(), await response.text()).toBe(201);
    const payload = await response.json();
    parts.push({ id: String(payload.part.id), number });
  }
  return { parts, sharedIdentifier };
}

async function lookup(page: Page, identifier: string) {
  const lookupRegion = page.getByRole("region", { name: "Part lookup" });
  const search = lookupRegion.getByRole("search", { name: "Part identifier lookup" });
  await search.getByLabel("Scan or enter part identifier").fill(identifier);
  const responsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/v1/inventory/parts/?identifier=") && response.request().method() === "GET",
  );
  await search.getByRole("button", { name: "Find part" }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBe(200);
  return lookupRegion;
}

test("scanner-wedge and manual lookup resolve a part, require ambiguity choice, and fall back clearly", async ({ page }, testInfo) => {
  await loginAs(page, "parts.clerk@example.com");
  const diagnostics = collectBrowserDiagnostics(page);
  try {
    const ambiguous = await createAmbiguousParts(page.context());
    await page.locator("aside").getByRole("link", { name: "Parts", exact: true }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Parts", exact: true })).toBeVisible();

    const exactLookup = await lookup(page, "DEMO-FIL-1001");
    await expect(exactLookup.getByRole("status")).toContainText("Selected FIL-1001 — Heavy-duty oil filter.");
    await expect(page.getByRole("heading", { name: "FIL-1001 history" })).toBeVisible();

    const ambiguousLookup = await lookup(page, ambiguous.sharedIdentifier);
    await expect(ambiguousLookup.getByRole("status")).toContainText("2 parts match");
    const chosen = ambiguous.parts[0];
    await ambiguousLookup.getByRole("group", { name: "Matching parts" })
      .getByRole("button", { name: new RegExp(chosen.number) }).click();
    await expect(ambiguousLookup.getByRole("status")).toContainText(`Selected ${chosen.number}`);

    const missing = `MISSING-${randomUUID()}`;
    const missingLookup = await lookup(page, missing);
    await expect(missingLookup.getByRole("alert")).toContainText(`No part matches “${missing}”.`);
    await expect(missingLookup.getByRole("alert")).toContainText("Use the part master below");

    await page.locator("aside").getByRole("link", { name: "Inventory", exact: true }).click();
    await lookup(page, "DEMO-FIL-1001");
    const known = await page.context().request.get(
      "/api/v1/inventory/parts/?identifier=DEMO-FIL-1001&active=true",
    );
    expect(known.status(), await known.text()).toBe(200);
    const knownPartId = String((await known.json()).parts[0].id);
    await expect(page.getByRole("combobox", { name: "Part", exact: true })).toHaveValue(knownPartId);
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});

test("part lookup hides another tenant and rejects a role without inventory access", async ({ browser }, testInfo) => {
  const clerkContext = await browser.newContext({ baseURL });
  const driverContext = await browser.newContext({ baseURL });
  const clerkPage = await clerkContext.newPage();
  const driverPage = await driverContext.newPage();
  const clerkDiagnostics = collectBrowserDiagnostics(clerkPage);
  const driverDiagnostics = collectBrowserDiagnostics(driverPage);
  try {
    await loginAs(clerkPage, "parts.clerk@example.com");
    await clerkPage.locator("aside").getByRole("link", { name: "Parts", exact: true }).click();
    const privateLookup = await lookup(clerkPage, "OTHER-PRIVATE-001");
    await expect(privateLookup.getByRole("alert")).toContainText("No part matches");
    const tenantLookup = await clerkContext.request.get(
      "/api/v1/inventory/parts/?identifier=OTHER-PRIVATE-001",
    );
    expect(tenantLookup.status(), await tenantLookup.text()).toBe(200);
    expect((await tenantLookup.json()).parts).toEqual([]);

    await loginAs(driverPage, "driver@example.com");
    const denied = await driverContext.request.get(
      "/api/v1/inventory/parts/?identifier=DEMO-FIL-1001",
    );
    expect(denied.status(), await denied.text()).toBe(403);
    await driverPage.goto("/parts");
    await expect(driverPage.getByRole("region", { name: "Part lookup" })).toHaveCount(0);
    clerkDiagnostics.assertClean();
    driverDiagnostics.assertClean();
  } finally {
    await Promise.all([
      clerkDiagnostics.attach(testInfo),
      driverDiagnostics.attach(testInfo),
    ]);
    await Promise.all([clerkContext.close(), driverContext.close()]);
  }
});
