import { createHmac } from "node:crypto";

import AxeBuilder from "@axe-core/playwright";
import { expect, test as base, type Page } from "@playwright/test";

import { collectBrowserDiagnostics } from "./helpers";

const password = process.env.E2E_PASSWORD ?? "DemoPass123!";
// JBSWY3DPEHPK3PXP, the deterministic seed-only MFA fixture, decoded as bytes.
const mfaKey = Buffer.from("48656c6c6f21deadbeef", "hex");

interface RolePage {
  role: string;
  username: string;
  path: string;
  heading: string;
  mfa?: boolean;
}

const rolePages: RolePage[] = [
  { role: "driver", username: "driver@example.com", path: "/report-problem", heading: "Report a defect" },
  { role: "technician", username: "technician@example.com", path: "/my-work", heading: "My work" },
  { role: "shop supervisor", username: "supervisor@example.com", path: "/work-orders", heading: "Work orders" },
  { role: "parts clerk", username: "parts.clerk@example.com", path: "/inventory", heading: "Inventory" },
  { role: "purchasing manager", username: "purchasing.manager@example.com", path: "/purchase-orders", heading: "Purchase orders" },
  { role: "fleet manager", username: process.env.E2E_USERNAME ?? "fleet.manager@example.com", path: "/reports", heading: "Reports" },
  { role: "company management", username: "management@example.com", path: "/", heading: "Overview" },
  { role: "system administrator", username: "system.admin@example.com", path: "/administration", heading: "Administration", mfa: true },
  { role: "integration administrator", username: "integration.admin@example.com", path: "/integrations", heading: "Devices and integrations", mfa: true },
];

const test = base.extend<{ diagnostics: ReturnType<typeof collectBrowserDiagnostics> }>({
  diagnostics: [
    async ({ page }, use, testInfo) => {
      const diagnostics = collectBrowserDiagnostics(page);
      try {
        await use(diagnostics);
        diagnostics.assertClean();
      } finally {
        await diagnostics.attach(testInfo);
      }
    },
    { auto: true },
  ],
});

function totp(): string {
  const message = Buffer.alloc(8);
  message.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30_000)));
  const digest = createHmac("sha1", mfaKey).update(message).digest();
  const offset = digest.at(-1)! & 0x0f;
  return ((digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000).toString().padStart(6, "0");
}

async function submitLogin(page: Page) {
  const response = page.waitForResponse(
    (candidate) => candidate.url().endsWith("/api/v1/auth/login/") && candidate.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  return response;
}

async function loginAs(page: Page, account: Pick<RolePage, "username" | "mfa">) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { level: 1, name: "Sign in", exact: true })).toBeVisible();
  await page.getByLabel("Username").fill(account.username);
  await page.getByLabel("Password").fill(password);

  let response = await submitLogin(page);
  if (account.mfa) {
    expect(response.status(), await response.text()).toBe(401);
    const otp = page.getByLabel("One-time code");
    await expect(otp).toBeVisible();
    await otp.fill(totp());
    response = await submitLogin(page);
  }
  expect(response.status(), await response.text()).toBe(200);
  await expect(page.locator("main")).toBeVisible();
}

async function expectLoadedPage(page: Page, heading: string) {
  await expect(page.getByRole("heading", { level: 1, name: heading, exact: true })).toBeVisible();
  await expect(page.locator(".loading")).toHaveCount(0);
}

async function expectNoAxeViolations(page: Page) {
  const { violations } = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  const failures = violations.map(({ id, impact, nodes }) => ({
      id,
      impact,
      nodes: nodes.map(({ target, failureSummary }) => ({ target, failureSummary })),
    }));
  expect(failures, "WCAG A/AA axe violations").toEqual([]);
}

for (const account of rolePages) {
  test(`@accessibility ${account.role} major page passes automated WCAG checks`, async ({ page }) => {
    await loginAs(page, account);
    await page.goto(account.path, { waitUntil: "domcontentloaded" });
    await expectLoadedPage(page, account.heading);
    await expectNoAxeViolations(page);
  });
}

test("@accessibility native validation focuses the first labeled invalid field", async ({ page }) => {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const username = page.getByLabel("Username");
  const passwordInput = page.getByLabel("Password");
  const submit = page.getByRole("button", { name: "Sign in", exact: true });

  await expect(username).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(passwordInput).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(submit).toBeFocused();
  await page.keyboard.press("Enter");

  await expect(username).toBeFocused();
  const validation = await username.evaluate((element: HTMLInputElement) => ({
    label: element.labels?.[0]?.textContent?.trim(),
    message: element.validationMessage,
    missing: element.validity.valueMissing,
  }));
  expect(validation.label).toContain("Username");
  expect(validation.message.length).toBeGreaterThan(0);
  expect(validation.missing).toBe(true);
  await expectNoAxeViolations(page);
});

test("@accessibility MFA validation announces the error, describes the code, and moves focus", async ({ page }) => {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.getByLabel("Username").fill("system.admin@example.com");
  await page.getByLabel("Password").fill(password);
  const response = await submitLogin(page);
  expect(response.status(), await response.text()).toBe(401);

  const alert = page.getByRole("alert");
  const otp = page.getByLabel("One-time code");
  await expect(alert).toContainText("A one-time code is required");
  await expect(otp).toBeFocused();
  const descriptionId = await otp.getAttribute("aria-describedby");
  expect(descriptionId).toBeTruthy();
  await expect(page.locator(`#${descriptionId}`)).toHaveText("Enter the six-digit code from your authenticator.");
  await expectNoAxeViolations(page);
});

test("@accessibility tabs support arrow-key navigation and expose selected state", async ({ page }) => {
  await loginAs(page, { username: "purchasing.manager@example.com" });
  await page.goto("/purchase-orders", { waitUntil: "domcontentloaded" });
  await expectLoadedPage(page, "Purchase orders");

  const ordersTab = page.getByRole("tab", { name: "Purchase orders", exact: true });
  const vendorsTab = page.getByRole("tab", { name: "Vendors", exact: true });
  await ordersTab.focus();
  await page.keyboard.press("End");
  await expect(vendorsTab).toBeFocused();
  await expect(vendorsTab).toHaveAttribute("aria-selected", "true");
  await expect(ordersTab).toHaveAttribute("tabindex", "-1");
  const panelId = await vendorsTab.getAttribute("aria-controls");
  expect(panelId).toBeTruthy();
  const selectedPanel = page.locator(`#${panelId}`);
  await expect(selectedPanel).toBeVisible();
  await expect(selectedPanel.getByLabel("Vendor code")).toBeVisible();
});

test("@accessibility work and synchronization states are conveyed with text", async ({ page }) => {
  await loginAs(page, { username: "technician@example.com" });
  await page.goto("/my-work", { waitUntil: "domcontentloaded" });
  await expectLoadedPage(page, "My work");
  await page.getByRole("link", { name: "WO-DEMO-0998", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: /WO-DEMO-0998/ })).toBeVisible();

  const completedTask = page.locator(".task-row").filter({ hasText: "Replace and verify right low-beam lamp" });
  await expect(completedTask).toContainText("Completed");
  await expect(page.getByTestId("sync-status").filter({ visible: true })).toContainText("Synchronized");
});

test("@accessibility mobile field workflow has labeled controls, text status, and managed navigation focus", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await loginAs(page, { username: "driver@example.com" });
  await page.goto("/report-problem", { waitUntil: "domcontentloaded" });
  await expectLoadedPage(page, "Report a defect");

  await expect(page.getByRole("combobox", { name: "Asset", exact: true })).toBeVisible();
  await expect(page.getByRole("group", { name: "What area?", exact: true })).toBeVisible();
  await expect(page.getByRole("group", { name: "Is it unsafe to drive?", exact: true })).toBeVisible();
  await expect(page.getByLabel("Describe it")).toBeVisible();
  await expect(page.getByLabel("Add photo or file")).toBeVisible();
  await expect(page.getByRole("button", { name: "Submit defect", exact: true })).toHaveAccessibleName("Submit defect");

  const selectedArea = page.getByRole("button", { name: "Other", exact: true });
  await expect(selectedArea).toHaveAttribute("aria-pressed", "true");
  const syncStatus = page.getByTestId("sync-status").filter({ visible: true });
  await expect(syncStatus).toContainText(/Synchronized|Saved on this device|Sync conflict/);
  await expect(syncStatus).toContainText(/All changes are up to date|Working offline|changes? pending/);

  const open = page.getByRole("button", { name: "Open navigation", exact: true });
  for (const target of [open, selectedArea, page.getByRole("button", { name: "Submit defect", exact: true })]) {
    const box = await target.boundingBox();
    expect(box, "critical mobile control has a rendered touch target").not.toBeNull();
    expect(box!.width).toBeGreaterThanOrEqual(44);
    expect(box!.height).toBeGreaterThanOrEqual(44);
  }
  const sidebar = page.locator('aside[aria-label="Primary navigation"]');
  await expect(open).toHaveAttribute("aria-expanded", "false");
  await expect(sidebar).toHaveAttribute("aria-hidden", "true");
  await open.focus();
  await page.keyboard.press("Enter");
  await expect(open).toHaveAttribute("aria-expanded", "true");
  await expect(sidebar).toHaveAttribute("aria-hidden", "false");
  const close = page.getByRole("button", { name: "Close navigation", exact: true });
  await expect(close).toBeFocused();
  const inspectLink = sidebar.getByRole("link", { name: "Inspect", exact: true });
  await inspectLink.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/inspections$/);
  await expect(page.getByRole("heading", { level: 1, name: "Inspections", exact: true })).toBeVisible();
  await expect(page.locator("#main-content")).toBeFocused();
  await expect(sidebar).toHaveAttribute("aria-hidden", "true");
  await expectNoAxeViolations(page);
});
