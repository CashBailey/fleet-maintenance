import { expect, type Page, test } from "@playwright/test";

import { collectBrowserDiagnostics } from "./helpers";

type Json = Record<string, unknown>;

const password = process.env.E2E_PASSWORD ?? "DemoPass123!";
const manager = process.env.E2E_USERNAME ?? "fleet.manager@example.com";
const technician = "technician@example.com";
const unit = "TRK-012";
const workSummary = "E2E transmission swap";
const removalReason = "Bench test only";
const taskTitle = "Fit replacement transmission";
const suffix = Date.now().toString(36).toUpperCase();
const serial = `ALLISON-${suffix}`;

function record(payload: Json | undefined, key: string): Json {
  const value = payload?.[key];
  expect(
    value && typeof value === "object" && !Array.isArray(value),
    `${key} must be an object`,
  ).toBeTruthy();
  return value as Json;
}

function rows(payload: Json, key: string): Json[] {
  const value = payload[key];
  expect(Array.isArray(value), `${key} must be an array`).toBe(true);
  return value as Json[];
}

function panel(page: Page, title: string) {
  return page.getByRole("heading", { level: 2, name: title, exact: true }).locator("..").locator("..");
}

async function loginAs(page: Page, username: string) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
  await page.getByLabel(/^Username/).fill(username);
  await page.getByLabel(/^Password/).fill(password);
  const responsePromise = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/auth/login/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBe(200);
  await expect(page.locator("main")).toBeVisible();
}

async function getJson(page: Page, path: string): Promise<Json> {
  const response = await page.context().request.get(path);
  expect(response.status(), await response.text()).toBe(200);
  return response.json() as Promise<Json>;
}

async function csrfToken(page: Page): Promise<string> {
  const response = await page.context().request.get("/api/v1/auth/csrf/");
  expect(response.status(), await response.text()).toBe(200);
  return String(((await response.json()) as Json).csrf_token);
}

async function postJson(
  page: Page,
  path: string,
  body: Json,
  key: string,
  expected = 201,
): Promise<Json> {
  const response = await page.context().request.post(path, {
    data: body,
    headers: { "Idempotency-Key": key, "X-CSRFToken": await csrfToken(page) },
  });
  expect(response.status(), await response.text()).toBe(expected);
  return response.json() as Promise<Json>;
}

test("component install, removal and reinstall are recorded as write-once periods", async ({
  browser,
}) => {
  const managerContext = await browser.newContext();
  const managerPage = await managerContext.newPage();
  const managerDiagnostics = collectBrowserDiagnostics(managerPage);
  const technicianContext = await browser.newContext();
  const technicianPage = await technicianContext.newPage();
  const technicianDiagnostics = collectBrowserDiagnostics(technicianPage);

  try {
    // --- Supervisor creates a work order on the seeded truck and assigns the technician.
    await loginAs(managerPage, manager);
    const assets = await getJson(managerPage, "/api/v1/assets/?q=" + unit);
    const asset = rows(assets, "assets").find((row) => row.unit_number === unit);
    expect(asset, `seeded asset ${unit} must exist`).toBeTruthy();
    const assetId = String(record({ asset }, "asset").id);

    const users = await getJson(managerPage, "/api/v1/users/?role=technician");
    const tech = rows(users, "users").find((row) => row.username === technician);
    expect(tech, `seeded technician ${technician} must exist`).toBeTruthy();

    const created = await postJson(
      managerPage,
      "/api/v1/maintenance/work-orders/",
      { asset_id: assetId, summary: workSummary, assigned_to_id: String(record({ tech }, "tech").id) },
      crypto.randomUUID(),
    );
    const workOrderId = String(record(created, "work_order").id);
    const workOrderNumber = String(record(created, "work_order").number);

    // --- Technician installs from the work-order screen.
    await loginAs(technicianPage, technician);
    await technicianPage.goto(`/work-orders/${workOrderId}`, { waitUntil: "domcontentloaded" });
    const componentsPanel = panel(technicianPage, `Components on ${unit}`);
    await expect(componentsPanel).toBeVisible();

    // The disclosure <summary> and the submit <button> both read "Install"; the
    // summary is the only one outside a form.
    await componentsPanel.locator("summary").filter({ hasText: "Install" }).click();
    const installForm = componentsPanel.locator("form");
    await expect(installForm).toBeVisible();
    await installForm.getByLabel(/^Kind/).selectOption("transmission");
    await installForm.getByLabel(/^Component serial number/).fill(serial);
    const installResponse = technicianPage.waitForResponse(
      (response) =>
        response.url().includes(`/api/v1/assets/${assetId}/components/`) &&
        response.request().method() === "POST",
    );
    await installForm.locator('button[type="submit"], button:not([type])').first().click();
    const installPayload = (await (await installResponse).json()) as Json;
    const installation = record(installPayload, "installation");
    const componentId = String(record(installPayload, "component").id);
    const firstInstallationId = String(installation.id);

    // Meters are captured automatically and the work order is recorded.
    expect(rows(installation, "installed_meters").length).toBeGreaterThanOrEqual(1);
    expect(installation.installed_work_order_id).toBe(workOrderId);
    expect(record(installation, "component").serial_number).toBe(serial);

    // --- A serial can only be on one truck at a time; a second install is refused.
    const conflict = await postJson(
      technicianPage,
      `/api/v1/assets/${assetId}/components/`,
      { component_id: componentId, work_order_id: workOrderId },
      crypto.randomUUID(),
      409,
    );
    expect(record(conflict, "error").code).toBe("component_installed_elsewhere");

    // --- Collapse the install popover; its fields sit over the Remove button.
    await componentsPanel.locator("summary").filter({ hasText: "Install" }).click();
    // A collapsed <details> hides its content; the form stays in the DOM.
    await expect(componentsPanel.locator("form")).toBeHidden();

    // --- Technician removes it with a reason.
    technicianPage.once("dialog", (dialog) => void dialog.accept(removalReason));
    const removeResponse = technicianPage.waitForResponse(
      (response) =>
        response.url().includes(`/api/v1/assets/components/${componentId}/remove/`) &&
        response.request().method() === "POST",
    );
    await componentsPanel.getByRole("button", { name: "Remove", exact: true }).click();
    const removePayload = (await (await removeResponse).json()) as Json;
    const closed = record(removePayload, "installation");
    expect(closed.id).toBe(firstInstallationId);
    expect(closed.removal_reason).toBe(removalReason);
    expect(closed.removed_work_order_id).toBe(workOrderId);

    // --- Reinstalling the same serial reuses the component and adds a second period.
    const reinstallKey = crypto.randomUUID();
    const reinstalled = await postJson(
      technicianPage,
      `/api/v1/assets/${assetId}/components/`,
      { component_id: componentId, work_order_id: workOrderId },
      reinstallKey,
    );
    const secondInstallationId = String(record(reinstalled, "installation").id);
    expect(secondInstallationId).not.toBe(firstInstallationId);

    // --- Replaying that exact key returns the same installation, not a third period.
    const replayed = await postJson(
      technicianPage,
      `/api/v1/assets/${assetId}/components/`,
      { component_id: componentId, work_order_id: workOrderId },
      reinstallKey,
    );
    expect(String(record(replayed, "installation").id)).toBe(secondInstallationId);

    // --- Tagging a task with the component needs maintenance.manage, so the supervisor does it.
    await postJson(
      managerPage,
      `/api/v1/maintenance/work-orders/${workOrderId}/tasks/`,
      { title: taskTitle, sequence: 90, component_id: componentId },
      crypto.randomUUID(),
    );

    // --- Supervisor sees both periods and the tagged task on the component page.
    await managerPage.goto(`/assets/${assetId}`, { waitUntil: "domcontentloaded" });
    const assetComponents = panel(managerPage, "Components");
    await expect(assetComponents).toContainText(serial);
    await expect(assetComponents.getByText("Past components (1)", { exact: false })).toBeVisible();

    await managerPage.goto(`/components/${componentId}`, { waitUntil: "domcontentloaded" });
    await expect(
      managerPage.getByRole("heading", { level: 1, name: `Transmission ${serial}`, exact: true }),
    ).toBeVisible();
    const historyPanel = panel(managerPage, "Installation history");
    await expect(historyPanel.locator("tbody tr")).toHaveCount(2);
    await expect(historyPanel).toContainText(removalReason);
    await expect(historyPanel).toContainText(workOrderNumber);
    await expect(panel(managerPage, "Service history")).toContainText(taskTitle);

    // --- The write-once record is what the API reports, and the audit names the technician.
    const detail = await getJson(managerPage, `/api/v1/assets/components/${componentId}/`);
    expect(rows(detail, "installations")).toHaveLength(2);
    expect(rows(detail, "tasks")).toHaveLength(1);

    const history = await getJson(managerPage, `/api/v1/assets/${assetId}/history/`);
    const timelineTypes = new Set(rows(history, "timeline").map((entry) => entry.type));
    expect(timelineTypes.has("component_installed")).toBe(true);
    expect(timelineTypes.has("component_removed")).toBe(true);

    managerDiagnostics.assertClean();
    technicianDiagnostics.assertClean();
  } finally {
    await technicianContext.close();
    await managerContext.close();
  }
});
