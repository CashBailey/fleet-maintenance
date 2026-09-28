import { createHmac, randomUUID } from "node:crypto";

import { expect, type Page, test } from "@playwright/test";

import { collectBrowserDiagnostics } from "./helpers";

const password = process.env.E2E_PASSWORD ?? "DemoPass123!";
const baseURL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:8088";
// JBSWY3DPEHPK3PXP, the deterministic seed-only MFA fixture, decoded as bytes.
const mfaKey = Buffer.from("48656c6c6f21deadbeef", "hex");

interface RoleCase {
  username: string;
  password?: string;
  home: string;
  navigation: string[];
  allowedLink: string;
  allowedHeading: string;
  mfa?: boolean;
}

const roles: Record<string, RoleCase> = {
  driver: {
    username: "driver@example.com",
    home: "Home",
    navigation: ["Home", "Inspect", "Report Problem", "My Reports"],
    allowedLink: "Report Problem",
    allowedHeading: "Report a defect",
  },
  technician: {
    username: "technician@example.com",
    home: "My work",
    navigation: ["My Work", "Assets", "Parts", "Inspections"],
    allowedLink: "Assets",
    allowedHeading: "Assets",
  },
  supervisor: {
    username: "supervisor@example.com",
    home: "Today",
    navigation: ["Today", "Work", "Assets", "Schedule", "Parts", "Alerts", "Reports"],
    allowedLink: "Work",
    allowedHeading: "Work orders",
  },
  "parts clerk": {
    username: "parts.clerk@example.com",
    home: "Parts",
    navigation: ["Parts", "Inventory", "Purchase Orders", "Vendors"],
    allowedLink: "Inventory",
    allowedHeading: "Inventory",
  },
  "purchasing manager": {
    username: "purchasing.manager@example.com",
    home: "Purchase orders",
    navigation: ["Inventory", "Purchase Orders", "Vendors", "Reports"],
    allowedLink: "Vendors",
    allowedHeading: "Vendors",
  },
  "fleet manager": {
    username: process.env.E2E_USERNAME ?? "fleet.manager@example.com",
    home: "Today",
    navigation: ["Today", "Work", "Assets", "Schedule", "Parts", "Reports"],
    allowedLink: "Reports",
    allowedHeading: "Reports",
  },
  "system administrator": {
    username: "system.admin@example.com",
    home: "System",
    navigation: ["System", "Administration", "Audit"],
    allowedLink: "Administration",
    allowedHeading: "Administration",
    mfa: true,
  },
  "integration administrator": {
    username: "integration.admin@example.com",
    home: "Integration health",
    navigation: ["Integration health", "Devices", "Data quality", "Audit"],
    allowedLink: "Devices",
    allowedHeading: "Devices and integrations",
    mfa: true,
  },
};

function totpForKey(key: Buffer): string {
  const message = Buffer.alloc(8);
  message.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30_000)));
  const digest = createHmac("sha1", key).update(message).digest();
  const offset = digest.at(-1)! & 0x0f;
  const value = (digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000;
  return value.toString().padStart(6, "0");
}

function totp(secret = ""): string {
  if (!secret) return totpForKey(mfaKey);
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const character of secret.toUpperCase().replace(/=+$/, "")) {
    const digit = alphabet.indexOf(character);
    if (digit < 0) throw new Error("Invalid base32 MFA secret");
    bits += digit.toString(2).padStart(5, "0");
  }
  const bytes = Buffer.alloc(Math.floor(bits.length / 8));
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = Number.parseInt(bits.slice(index * 8, index * 8 + 8), 2);
  }
  return totpForKey(bytes);
}

async function submitLogin(page: Page) {
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/login/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  return responsePromise;
}

async function loginAs(page: Page, account: RoleCase | { username: string; password?: string; mfa?: boolean }) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
  await page.getByLabel(/^Username/).fill(account.username);
  await page.getByLabel(/^Password/).fill(account.password ?? password);

  let response = await submitLogin(page);
  if (account.mfa) {
    expect(response.status(), await response.text()).toBe(401);
    await expect(page.getByLabel(/^One-time code/)).toBeVisible();
    await page.getByLabel(/^One-time code/).fill(totp());
    response = await submitLogin(page);
  }
  expect(response.status(), await response.text()).toBe(200);
  await expect(page.locator("main")).toBeVisible();
}

async function logoutThroughUi(page: Page) {
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/logout/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBe(200);
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
  expect((await page.context().request.get("/api/v1/auth/me/")).status()).toBe(403);
}

async function csrfToken(page: Page): Promise<string> {
  const response = await page.context().request.get("/api/v1/auth/csrf/");
  expect(response.status(), await response.text()).toBe(200);
  return String((await response.json()).csrf_token);
}

async function expectMutation(
  page: Page,
  path: string,
  action: () => Promise<unknown>,
): Promise<void> {
  const responsePromise = page.waitForResponse(
    (response) => new URL(response.url()).pathname === path && response.request().method() === "POST",
  );
  await action();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBeLessThan(300);
}

async function expectOfflineMutation(page: Page, action: () => Promise<unknown>): Promise<void> {
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/offline/sync/") && response.request().method() === "POST",
  );
  await action();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBe(200);
  expect(await response.json()).toMatchObject({ results: [{ status: "synced" }] });
}

async function exerciseAllowedMutation(page: Page, role: string, suffix: string): Promise<void> {
  const navigation = page.locator('aside[aria-label="Primary navigation"] nav');

  if (role === "driver") {
    await page.getByLabel(/^Asset\b/).selectOption({ index: 1 });
    await page.getByLabel(/^Describe it\b/).fill(`RBAC driver defect ${suffix}`);
    await expectOfflineMutation(page, () => page.getByRole("button", { name: "Submit defect", exact: true }).click());
    await expect(page.getByText(/Saved on this device\./)).toBeVisible();
    return;
  }

  if (role === "technician") {
    await navigation.getByRole("link", { name: "My Work", exact: true }).click();
    await page.getByRole("link", { name: "WO-DEMO-1001", exact: true }).click();
    await page.getByLabel(/^Note\b/).fill(`RBAC technician note ${suffix}`);
    await expectOfflineMutation(page, () => page.getByRole("button", { name: "Save work note", exact: true }).click());
    return;
  }

  if (role === "supervisor") {
    await navigation.getByRole("link", { name: "Schedule", exact: true }).click();
    const refreshResponse = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/v1/maintenance/plans/"
        && response.request().method() === "GET",
    );
    await expectMutation(page, "/api/v1/maintenance/plans/recalculate/", () =>
      page.getByRole("button", { name: "Recalculate due status", exact: true }).click());
    const refreshed = await refreshResponse;
    expect(refreshed.status(), await refreshed.text()).toBe(200);
    return;
  }

  if (role === "parts clerk") {
    const partNumber = `RBAC-${suffix}`.toUpperCase();
    await navigation.getByRole("link", { name: "Parts", exact: true }).click();
    await page.getByText("New part", { exact: true }).click();
    await page.getByLabel(/^Part number\b/).fill(partNumber);
    await page.getByLabel(/^Part name\b/).fill(`RBAC part ${suffix}`);
    await expectMutation(page, "/api/v1/inventory/parts/", () =>
      page.getByRole("button", { name: "Create part", exact: true }).click());
    await expect(page.getByRole("button", { name: partNumber, exact: true })).toBeVisible();
    return;
  }

  if (role === "purchasing manager") {
    const vendorCode = `RBAC-${suffix}`.toUpperCase();
    await page.getByLabel(/^Vendor code\b/).fill(vendorCode);
    await page.getByLabel(/^Vendor name\b/).fill(`RBAC vendor ${suffix}`);
    await page.getByLabel("Email", { exact: true }).fill(`rbac.vendor.${suffix}@example.invalid`);
    await expectMutation(page, "/api/v1/purchasing/vendors/", () =>
      page.getByRole("button", { name: "Create vendor", exact: true }).click());
    await expect(page.getByText(new RegExp(`${vendorCode} · RBAC vendor ${suffix}`))).toBeVisible();
    return;
  }

  if (role === "fleet manager") {
    const unitNumber = `RBAC-${suffix}`.toUpperCase();
    await navigation.getByRole("link", { name: "Assets", exact: true }).click();
    await page.getByText("New asset", { exact: true }).click();
    await page.getByLabel(/^Unit number\b/).fill(unitNumber);
    await expectMutation(page, "/api/v1/assets/", () =>
      page.getByRole("button", { name: "Create asset", exact: true }).click());
    await expect(page.getByRole("link", { name: unitNumber, exact: true })).toBeVisible();
    return;
  }

  if (role === "system administrator") {
    const locationCode = `RBAC-${suffix}`.toUpperCase();
    await page.getByLabel(/^Location code\b/).fill(locationCode);
    await page.getByLabel(/^Location name\b/).fill(`RBAC location ${suffix}`);
    await expectMutation(page, "/api/v1/locations/", () =>
      page.getByRole("button", { name: "Create location", exact: true }).click());
    await expect(page.getByRole("strong").filter({ hasText: `${locationCode} · RBAC location ${suffix}` })).toBeVisible();
    return;
  }

  if (role === "integration administrator") {
    const serial = `RBAC-${suffix}`;
    await page.getByLabel(/^Device name\b/).fill(`RBAC device ${suffix}`);
    await page.getByLabel(/^Model\b/).fill("E2E");
    await page.getByLabel(/^Serial number\b/).fill(serial);
    await page.getByLabel(/^External device ID\b/).fill(`rbac-${suffix}`);
    await expectMutation(page, "/api/v1/integrations/devices/", () =>
      page.getByRole("button", { name: "Register device", exact: true }).click());
    await expect(page.getByText(new RegExp(`${serial} ·`))).toBeVisible();
  }
}

for (const [role, account] of Object.entries(roles)) {
  test(`@rbac ${role} signs in, sees only role navigation, performs an allowed mutation, and signs out`, async ({ page }, testInfo) => {
    await loginAs(page, account);
    const diagnostics = collectBrowserDiagnostics(page);
    try {
      await expect(page.getByRole("heading", { level: 1, name: account.home, exact: true })).toBeVisible();

      const navigation = page.locator('aside[aria-label="Primary navigation"] nav');
      await expect(navigation).toBeVisible();
      await expect.poll(async () => navigation.locator("a span").allTextContents()).toEqual(account.navigation);
      await expect(navigation.getByRole("link", { name: "Administration", exact: true })).toHaveCount(
        role === "system administrator" ? 1 : 0,
      );

      await navigation.getByRole("link", { name: account.allowedLink, exact: true }).click();
      await expect(page.getByRole("heading", { level: 1, name: account.allowedHeading, exact: true })).toBeVisible();
      await expect(page.getByText("Access denied", { exact: true })).toHaveCount(0);

      await exerciseAllowedMutation(page, role, randomUUID().slice(0, 8));

      diagnostics.assertClean();
      await logoutThroughUi(page);
    } finally {
      await diagnostics.attach(testInfo);
    }
  });
}

test("@rbac unauthorized controls are hidden and a direct mutation is rejected server-side", async ({ page }, testInfo) => {
  await loginAs(page, roles.driver);
  const diagnostics = collectBrowserDiagnostics(page);
  try {
    await expect(page.getByRole("link", { name: "Work", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Create asset", exact: true })).toHaveCount(0);

    const response = await page.context().request.post("/api/v1/assets/", {
      headers: {
        "X-CSRFToken": await csrfToken(page),
        "Idempotency-Key": randomUUID(),
      },
      data: { unit_number: "RBAC-FORBIDDEN", asset_type: "Truck" },
    });
    expect(response.status(), await response.text()).toBe(403);
    expect(await response.json()).toMatchObject({ error: { code: "permission_denied" } });

    const assets = await page.context().request.get("/api/v1/assets/?q=RBAC-FORBIDDEN");
    expect(assets.status(), await assets.text()).toBe(200);
    expect((await assets.json()).assets).toEqual([]);
    await logoutThroughUi(page);
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});

test("@rbac parts staff can use stock locations without seeing or setting financial values", async ({ page }, testInfo) => {
  const suffix = randomUUID().slice(0, 8).toUpperCase();
  await loginAs(page, roles["parts clerk"]);
  const diagnostics = collectBrowserDiagnostics(page);
  try {
    const navigation = page.locator('aside[aria-label="Primary navigation"] nav');
    await navigation.getByRole("link", { name: "Inventory", exact: true }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Inventory", exact: true })).toBeVisible();
    const stockRow = page.getByRole("heading", { level: 2, name: "Stock on hand", exact: true })
      .locator("..")
      .locator("..")
      .getByRole("row")
      .filter({ hasText: "FIL-1001" });
    await expect(stockRow).toContainText("MAIN/A-01");

    await navigation.getByRole("link", { name: "Parts", exact: true }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Parts", exact: true })).toBeVisible();
    await expect(page.getByRole("columnheader", { name: "Standard cost", exact: true })).toHaveCount(0);
    await expect(page.getByLabel("Standard cost", { exact: true })).toHaveCount(0);

    const allowedNumber = `RBAC-NOCOST-${suffix}`;
    await page.getByText("New part", { exact: true }).click();
    await page.getByLabel(/^Part number\b/).fill(allowedNumber);
    await page.getByLabel(/^Part name\b/).fill(`No-cost operational part ${suffix}`);
    const allowedCreate = page.waitForResponse(
      (response) => response.url().endsWith("/api/v1/inventory/parts/") && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Create part", exact: true }).click();
    const allowedResponse = await allowedCreate;
    expect(allowedResponse.status(), await allowedResponse.text()).toBe(201);
    const allowedPart = (await allowedResponse.json()).part as Record<string, unknown>;
    expect(allowedPart).toMatchObject({ number: allowedNumber });
    expect(allowedPart).not.toHaveProperty("default_unit_cost");

    const clerkParts = await page.context().request.get(
      "/api/v1/inventory/parts/?identifier=FIL-1001",
    );
    expect(clerkParts.status(), await clerkParts.text()).toBe(200);
    const clerkPart = (await clerkParts.json()).parts[0] as Record<string, unknown>;
    expect(clerkPart).toMatchObject({ number: "FIL-1001", name: "Heavy-duty oil filter" });
    expect(clerkPart).not.toHaveProperty("default_unit_cost");

    const forbiddenNumber = `RBAC-COST-${suffix}`;
    const denied = await page.context().request.post("/api/v1/inventory/parts/", {
      headers: {
        "X-CSRFToken": await csrfToken(page),
        "Idempotency-Key": randomUUID(),
      },
      data: {
        number: forbiddenNumber,
        name: `Forbidden cost part ${suffix}`,
        default_unit_cost: "42.00",
      },
    });
    expect(denied.status(), await denied.text()).toBe(403);
    expect(await denied.json()).toMatchObject({ error: { code: "financial_permission_denied" } });
    const absent = await page.context().request.get(
      `/api/v1/inventory/parts/?identifier=${encodeURIComponent(forbiddenNumber)}`,
    );
    expect(absent.status(), await absent.text()).toBe(200);
    expect((await absent.json()).parts).toEqual([]);

    await logoutThroughUi(page);
    await loginAs(page, roles["fleet manager"]);
    await page.locator('aside[aria-label="Primary navigation"] nav').getByRole("link", { name: "Parts", exact: true }).click();
    await expect(page.getByRole("columnheader", { name: "Standard cost", exact: true })).toBeVisible();
    await expect(page.getByRole("row").filter({ hasText: "FIL-1001" })).toContainText("28.7500");
    const financialParts = await page.context().request.get(
      "/api/v1/inventory/parts/?identifier=FIL-1001",
    );
    expect(financialParts.status(), await financialParts.text()).toBe(200);
    expect((await financialParts.json()).parts[0]).toMatchObject({
      number: "FIL-1001",
      default_unit_cost: "28.7500",
    });

    await logoutThroughUi(page);
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});

test("@rbac organization data is isolated in UI search and direct object access", async ({ browser }, testInfo) => {
  const mainContext = await browser.newContext({ baseURL });
  const otherContext = await browser.newContext({ baseURL });
  const mainPage = await mainContext.newPage();
  const otherPage = await otherContext.newPage();
  let mainDiagnostics: ReturnType<typeof collectBrowserDiagnostics> | undefined;
  let otherDiagnostics: ReturnType<typeof collectBrowserDiagnostics> | undefined;
  try {
    await loginAs(mainPage, roles["fleet manager"]);
    await loginAs(otherPage, { username: "other.manager@example.com" });
    mainDiagnostics = collectBrowserDiagnostics(mainPage);
    otherDiagnostics = collectBrowserDiagnostics(otherPage);
    const mainBootstrap = await mainContext.request.get("/api/v1/bootstrap/");
    expect(mainBootstrap.status(), await mainBootstrap.text()).toBe(200);
    const mainAssetId = String((await mainBootstrap.json()).assets[0].id);

    const searchResponse = mainPage.waitForResponse(
      (response) => response.url().includes("/api/v1/search/?q=OTHER-001"),
    );
    await mainPage.getByLabel("Search assets, work orders, parts, vendors, and components", { exact: true }).first().fill("OTHER-001");
    await mainPage.getByLabel("Search assets, work orders, parts, vendors, and components", { exact: true }).first().press("Enter");
    expect((await searchResponse).status()).toBe(200);
    await expect(mainPage.getByText("No matching records found.", { exact: true })).toBeVisible();

    const privateSearch = await mainContext.request.get("/api/v1/search/?q=PRIVATE-ONLY");
    expect(privateSearch.status(), await privateSearch.text()).toBe(200);
    expect((await privateSearch.json()).results).toEqual([]);

    const otherBootstrap = await otherContext.request.get("/api/v1/bootstrap/");
    expect(otherBootstrap.status(), await otherBootstrap.text()).toBe(200);
    const otherAssetId = String((await otherBootstrap.json()).assets[0].id);

    expect((await mainContext.request.get(`/api/v1/assets/${otherAssetId}/`)).status()).toBe(404);
    expect((await otherContext.request.get(`/api/v1/assets/${mainAssetId}/`)).status()).toBe(404);

    await logoutThroughUi(mainPage);
    await logoutThroughUi(otherPage);
    mainDiagnostics.assertClean();
    otherDiagnostics.assertClean();
  } finally {
    if (mainDiagnostics) await mainDiagnostics.attach(testInfo);
    if (otherDiagnostics) await otherDiagnostics.attach(testInfo);
    await mainContext.close();
    await otherContext.close();
  }
});

test("@rbac disabling a user revokes its current session and prevents another login", async ({ browser }, testInfo) => {
  const disposableUsername = `rbac.management.${randomUUID()}@example.invalid`;
  const disposablePassword = "Violet7!Harbor92$Quartz";
  const userContext = await browser.newContext({ baseURL });
  const adminContext = await browser.newContext({ baseURL });
  const userPage = await userContext.newPage();
  const adminPage = await adminContext.newPage();
  let adminDiagnostics: ReturnType<typeof collectBrowserDiagnostics> | undefined;
  try {
    await loginAs(adminPage, roles["system administrator"]);
    adminDiagnostics = collectBrowserDiagnostics(adminPage);
    await adminPage.getByRole("link", { name: "Administration", exact: true }).first().click();
    await expect(adminPage.getByRole("heading", { level: 1, name: "Administration", exact: true })).toBeVisible();

    await adminPage.getByLabel(/^First name\b/).fill("RBAC");
    await adminPage.getByLabel(/^Last name\b/).fill("Disposable");
    await adminPage.getByLabel(/^Username or email\b/).fill(disposableUsername);
    await adminPage.getByLabel(/^Temporary password\b/).fill(disposablePassword);
    await adminPage.getByLabel(/^Role\b/).selectOption("management");
    const createResponse = adminPage.waitForResponse(
      (response) => response.url().endsWith("/api/v1/users/") && response.request().method() === "POST",
    );
    await adminPage.getByRole("button", { name: "Create user", exact: true }).click();
    const created = await createResponse;
    expect(created.status(), await created.text()).toBe(201);
    expect(await created.json()).toMatchObject({
      user: { username: disposableUsername, roles: ["management"], active: true },
    });
    await expect(adminPage.getByText("User created with the selected role and location.", { exact: true })).toBeVisible();
    const userRow = adminPage.locator(".workflow-row").filter({ hasText: disposableUsername });
    await expect(userRow).toBeVisible();

    await loginAs(userPage, { username: disposableUsername, password: disposablePassword });
    await expect(userPage.getByRole("heading", { level: 1, name: "Overview", exact: true })).toBeVisible();

    adminPage.once("dialog", (dialog) => void dialog.accept());
    const disableResponse = adminPage.waitForResponse(
      (response) => response.url().includes("/api/v1/users/") && response.url().endsWith("/disable/") && response.request().method() === "POST",
    );
    await userRow.getByRole("button", { name: "Disable user", exact: true }).click();
    const disabled = await disableResponse;
    expect(disabled.status(), await disabled.text()).toBe(200);
    expect(await disabled.json()).toMatchObject({ user: { active: false } });
    await expect(adminPage.getByText("User disabled and access revoked.", { exact: true })).toBeVisible();

    expect((await userContext.request.get("/api/v1/auth/me/")).status()).toBe(403);
    await userPage.reload({ waitUntil: "domcontentloaded" });
    await expect(userPage.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
    await userPage.getByLabel(/^Username/).fill(disposableUsername);
    await userPage.getByLabel(/^Password/).fill(disposablePassword);
    const rejected = await submitLogin(userPage);
    expect(rejected.status(), await rejected.text()).toBe(401);
    await expect(userPage.getByText("Authentication failed", { exact: true })).toBeVisible();

    await logoutThroughUi(adminPage);
    adminDiagnostics.assertClean();
  } finally {
    if (adminDiagnostics) await adminDiagnostics.attach(testInfo);
    await userContext.close();
    await adminContext.close();
  }
});

test("@rbac privileged user MFA is provisioned once, rotated with a reason, and required at login", async ({ browser }, testInfo) => {
  const username = `rbac.integration.${randomUUID()}@example.invalid`;
  const userPassword = "Violet7!Harbor92$Quartz";
  const adminContext = await browser.newContext({ baseURL });
  const adminPage = await adminContext.newPage();
  let adminDiagnostics: ReturnType<typeof collectBrowserDiagnostics> | undefined;
  try {
    await loginAs(adminPage, roles["system administrator"]);
    adminDiagnostics = collectBrowserDiagnostics(adminPage);
    await adminPage.getByRole("link", { name: "Administration", exact: true }).first().click();
    await adminPage.getByLabel(/^First name\b/).fill("MFA");
    await adminPage.getByLabel(/^Last name\b/).fill("Operator");
    await adminPage.getByLabel(/^Username or email\b/).fill(username);
    await adminPage.getByLabel(/^Temporary password\b/).fill(userPassword);
    await adminPage.getByLabel(/^Role\b/).selectOption("integration_admin");
    const createPromise = adminPage.waitForResponse(
      (response) => response.url().endsWith("/api/v1/users/") && response.request().method() === "POST",
    );
    await adminPage.getByRole("button", { name: "Create user", exact: true }).click();
    const created = await createPromise;
    expect(created.status(), await created.text()).toBe(201);
    const createdBody = await created.json() as { mfa_provisioning: { secret: string; otpauth_uri: string }; secret_recoverable: boolean };
    const firstSecret = createdBody.mfa_provisioning.secret;
    expect(createdBody.secret_recoverable).toBe(true);
    await expect(adminPage.getByRole("heading", { name: "Copy this MFA setup now", exact: true })).toBeVisible();
    await expect(adminPage.getByText(firstSecret, { exact: true })).toBeVisible();
    expect(createdBody.mfa_provisioning.otpauth_uri).toContain("otpauth://totp/");

    const userContext = await browser.newContext({ baseURL });
    const userPage = await userContext.newPage();
    try {
      await userPage.goto("/", { waitUntil: "domcontentloaded" });
      await userPage.getByLabel(/^Username/).fill(username);
      await userPage.getByLabel(/^Password/).fill(userPassword);
      expect((await submitLogin(userPage)).status()).toBe(401);
      await userPage.getByLabel(/^One-time code/).fill(totp(firstSecret));
      expect((await submitLogin(userPage)).status()).toBe(200);
      await expect(userPage.getByRole("heading", { level: 1, name: "Integration health", exact: true })).toBeVisible();
      await logoutThroughUi(userPage);
    } finally {
      await userContext.close();
    }

    const userRow = adminPage.locator(".workflow-row").filter({ hasText: username });
    await expect(userRow.getByText("MFA configured", { exact: true })).toBeVisible();
    adminPage.once("dialog", (dialog) => void dialog.accept("Authenticator replaced during E2E"));
    const rotatePromise = adminPage.waitForResponse(
      (response) => response.url().includes("/api/v1/users/") && response.url().endsWith("/mfa/") && response.request().method() === "POST",
    );
    await userRow.getByRole("button", { name: "Reset MFA", exact: true }).click();
    const rotated = await rotatePromise;
    expect(rotated.status(), await rotated.text()).toBe(200);
    const rotatedBody = await rotated.json() as { mfa_provisioning: { secret: string }; secret_recoverable: boolean };
    const secondSecret = rotatedBody.mfa_provisioning.secret;
    expect(secondSecret).not.toBe(firstSecret);
    await expect(adminPage.getByText(secondSecret, { exact: true })).toBeVisible();

    const reloginContext = await browser.newContext({ baseURL });
    const reloginPage = await reloginContext.newPage();
    try {
      await reloginPage.goto("/", { waitUntil: "domcontentloaded" });
      await reloginPage.getByLabel(/^Username/).fill(username);
      await reloginPage.getByLabel(/^Password/).fill(userPassword);
      expect((await submitLogin(reloginPage)).status()).toBe(401);
      await reloginPage.getByLabel(/^One-time code/).fill(totp(firstSecret));
      expect((await submitLogin(reloginPage)).status()).toBe(401);
      await reloginPage.getByLabel(/^One-time code/).fill(totp(secondSecret));
      expect((await submitLogin(reloginPage)).status()).toBe(200);
      const diagnostics = collectBrowserDiagnostics(reloginPage);
      await expect(reloginPage.getByRole("heading", { level: 1, name: "Integration health", exact: true })).toBeVisible();
      diagnostics.assertClean();
      await diagnostics.attach(testInfo);
    } finally {
      await reloginContext.close();
    }

    const audit = await adminContext.request.get(`/api/v1/audit-events/?resource_type=User`);
    expect(audit.status(), await audit.text()).toBe(200);
    const events = (await audit.json()).events as Array<{ action: string; context: Record<string, unknown> }>;
    expect(events.some((event) => event.action === "user.created" && event.context.mfa_provisioned === true)).toBe(true);
    expect(events.some((event) => event.action === "user.mfa.rotated" && event.context.reason === "Authenticator replaced during E2E")).toBe(true);
    expect(JSON.stringify(events)).not.toContain(firstSecret);
    expect(JSON.stringify(events)).not.toContain(secondSecret);
    adminDiagnostics.assertClean();
  } finally {
    if (adminDiagnostics) await adminDiagnostics.attach(testInfo);
    await adminContext.close();
  }
});
