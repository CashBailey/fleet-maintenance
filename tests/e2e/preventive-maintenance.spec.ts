import { createHash } from "node:crypto";

import { expect, type Page, test } from "@playwright/test";

import { collectBrowserDiagnostics, e2eUser, loginThroughUi } from "./helpers";

type Json = Record<string, unknown>;

const password = process.env.E2E_PASSWORD ?? "DemoPass123!";

function record(value: unknown): Json {
  expect(value).toBeTruthy();
  expect(typeof value).toBe("object");
  expect(Array.isArray(value)).toBe(false);
  return value as Json;
}

async function mutate(
  page: Page,
  method: string,
  path: string,
  action: () => Promise<unknown>,
): Promise<Json> {
  const responsePromise = page.waitForResponse((response) => {
    return new URL(response.url()).pathname === path && response.request().method() === method;
  });
  await action();
  const response = await responsePromise;
  const body = await response.text();
  expect(response.status(), body).toBeLessThan(300);
  return body ? record(JSON.parse(body)) : {};
}

async function loginAs(page: Page, username: string) {
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
  await page.getByLabel(/^Username\b/).fill(username);
  await page.getByLabel(/^Password\b/).fill(password);
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/login/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBe(200);
  await expect(page.locator("main")).toBeVisible();
}

async function logout(page: Page) {
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/logout/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBe(200);
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
}

async function openNavigation(page: Page, name: string, heading: string) {
  const navigation = page.locator('aside[aria-label="Primary navigation"]');
  await navigation.getByRole("link", { name, exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: heading, exact: true })).toBeVisible();
}

function planRow(page: Page, planName: string) {
  return page.getByText(new RegExp(`^${planName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")} · next due`))
    .locator("..").locator("..");
}

test("@pm mileage preventive maintenance creates due work and advances the preserved package", async ({ page }, testInfo) => {
  const suffix = createHash("sha256").update(testInfo.testId).digest("hex").slice(0, 8).toUpperCase();
  const unit = `PM-${suffix}`;
  const vin = `1PM${suffix}000000`;
  const make = `Fleetline ${suffix}`;
  const model = `Model ${suffix}`;
  const engineType = `Diesel-${suffix}`;
  const transmission = `Transmission-${suffix}`;
  const axleConfiguration = "6x4 tandem";
  const axleRatio = "3.55";
  const packageName = `1,000 Mile Service ${suffix}`;
  const planName = `Mileage PM ${suffix}`;
  const taskName = `Perform PM task ${suffix}`;
  const replacementTask = `Future package task ${suffix}`;
  const completionSummary = `Completed scheduled PM ${suffix}`;

  await loginThroughUi(page);
  const diagnostics = collectBrowserDiagnostics(page);
  try {

    await openNavigation(page, "Assets", "Assets");
    await page.getByText("New asset", { exact: true }).click();
    const assetForm = page.getByRole("button", { name: "Create asset", exact: true }).locator("xpath=ancestor::form");
    await expect(assetForm.getByLabel(/^Location\b/)).toHaveCount(0);
    await assetForm.getByLabel(/^Unit number\b/).fill(unit);
    await assetForm.getByLabel(/^VIN\b/).fill(vin);
    await assetForm.getByLabel(/^Make\b/).fill(make);
    await assetForm.getByLabel(/^Model\b/).fill(model);
    await assetForm.getByLabel(/^Truck type\b/).fill("Truck");
    await assetForm.getByLabel(/^Engine type\b/).fill(engineType);
    // Engine serial numbers are Components now (ADR 0011); the create form no longer asks.
    await expect(assetForm.getByLabel(/^Engine serial number\b/)).toHaveCount(0);
    const assetPayload = await mutate(page, "POST", "/api/v1/assets/", () =>
      page.getByRole("button", { name: "Create asset", exact: true }).click(),
    );
    const asset = record(assetPayload.asset);
    const assetId = String(asset.id);
    expect(asset).toMatchObject({
      unit_number: unit,
      vin,
      make,
      model,
      home_location_id: null,
      specs: { equipment: { engine: { type: engineType } } },
    });
    await expect(page.getByRole("link", { name: unit, exact: true })).toBeVisible();

    await page.getByRole("link", { name: unit, exact: true }).click();
    await expect(page.getByRole("heading", { level: 1, name: unit, exact: true })).toBeVisible();
    const assetDetails = page.getByRole("heading", { level: 2, name: "Asset details", exact: true }).locator("xpath=ancestor::section");
    await expect(assetDetails).toContainText(vin);
    await expect(assetDetails).toContainText(make);
    await expect(assetDetails).toContainText(model);
    await expect(assetDetails.locator("dt").filter({ hasText: /^Location$/ })).toHaveCount(0);
    const engineDetails = page.getByRole("heading", { level: 2, name: "Engine information", exact: true }).locator("xpath=ancestor::section");
    await expect(engineDetails).toContainText(engineType);
    // No engine component is installed, so the serial row reads as empty.
    await expect(engineDetails.locator("dt").filter({ hasText: /^Engine serial number$/ })).toHaveCount(1);
    await page.getByText("Edit equipment details", { exact: true }).click();
    const equipmentForm = page.getByRole("button", { name: "Save equipment details", exact: true }).locator("xpath=ancestor::form");
    await equipmentForm.getByLabel("Transmission manufacturer", { exact: true }).fill(transmission);
    await equipmentForm.locator('input[name="axle_configuration"]').fill(axleConfiguration);
    await equipmentForm.getByLabel("Axle ratio", { exact: true }).fill(axleRatio);
    const equipmentPayload = await mutate(page, "PATCH", `/api/v1/assets/${assetId}/`, () =>
      page.getByRole("button", { name: "Save equipment details", exact: true }).click(),
    );
    expect(record(equipmentPayload.asset).specs).toMatchObject({
      equipment: {
        engine: { type: engineType },
        transmission: { manufacturer: transmission },
        axle: { configuration: axleConfiguration, ratio: axleRatio },
      },
    });
    const equipmentDetails = page.getByRole("heading", { level: 2, name: "Equipment details", exact: true }).locator("xpath=ancestor::section");
    await expect(equipmentDetails).toContainText(transmission);
    await expect(equipmentDetails).toContainText(axleConfiguration);
    await expect(equipmentDetails).toContainText(axleRatio);
    await page.getByRole("combobox", { name: "New meter type", exact: true }).selectOption("odometer");
    await page.getByRole("combobox", { name: "Unit", exact: true }).selectOption("mi");
    await page.getByLabel(/^Reading\b/).fill("400");
    const firstReadingPayload = await mutate(page, "POST", `/api/v1/assets/${assetId}/meters/`, () =>
      page.getByRole("button", { name: "Record meter", exact: true }).click(),
    );
    const meterId = String(record(firstReadingPayload.meter).id);
    await expect(page.getByRole("cell", { name: /400(?:\.0+)? mi/ })).toBeVisible();

    await openNavigation(page, "Schedule", "Schedule");
    await page.getByRole("tab", { name: "Service packages", exact: true }).click();
    await page.getByLabel(/^Package name\b/).fill(packageName);
    await page.getByLabel(/^Description\b/).fill("Deterministic mileage preventive maintenance package");
    await page.getByLabel(/^Required task\b/).fill(taskName);
    const packagePayload = await mutate(page, "POST", "/api/v1/maintenance/service-packages/", () =>
      page.getByRole("button", { name: "Create package", exact: true }).click(),
    );
    const servicePackage = record(packagePayload.service_package);
    expect(servicePackage.version).toBe(1);
    await expect(page.getByText(packageName, { exact: true })).toBeVisible();

    await page.getByRole("tab", { name: "Maintenance plans", exact: true }).click();
    await page.getByLabel(/^Plan name\b/).fill(planName);
    await page.getByRole("combobox", { name: "Asset", exact: true }).selectOption({ label: unit });
    await page.getByRole("combobox", { name: "Service package", exact: true }).selectOption({ label: `${packageName} · v1` });
    await page.getByRole("combobox", { name: "Trigger", exact: true }).selectOption("mileage");
    await page.getByRole("combobox", { name: "Meter", exact: true }).selectOption(meterId);
    await page.getByRole("spinbutton", { name: /^Last completed reading/ }).fill("0");
    await page.getByLabel(/^Interval\b/).fill("1000");
    await page.getByLabel(/^Grace\b/).fill("100");
    const planPayload = await mutate(page, "POST", "/api/v1/maintenance/plans/", () =>
      page.getByRole("button", { name: "Create plan", exact: true }).click(),
    );
    const plan = record(planPayload.plan);
    const planId = String(plan.id);
    expect(plan.due_status).toBe("Current");
    await expect(planRow(page, planName)).toContainText("Current");

    await openNavigation(page, "Assets", "Assets");
    await page.getByRole("link", { name: unit, exact: true }).click();
    await page.getByRole("combobox", { name: "Existing meter", exact: true }).selectOption(meterId);
    await page.getByLabel(/^Reading\b/).fill("1200");
    const dueReadingPayload = await mutate(page, "POST", `/api/v1/assets/${assetId}/meters/`, () =>
      page.getByRole("button", { name: "Record meter", exact: true }).click(),
    );
    const dueReadingId = String(record(dueReadingPayload.reading).id);

    let workerPlan: Json | undefined;
    await expect.poll(async () => {
      const response = await page.context().request.get(`/api/v1/maintenance/plans/?asset_id=${assetId}`);
      if (response.status() !== 200) return { status: `HTTP ${response.status()}`, currentValue: null, dueValue: null };
      const payload = record(await response.json());
      const candidate = (payload.plans as unknown[]).find((item) => record(item).id === planId);
      if (!candidate) return { status: "Missing", currentValue: null, dueValue: null };
      workerPlan = record(candidate);
      const reason = record((workerPlan.due_reasons as unknown[])[0]);
      return {
        status: workerPlan.due_status,
        currentValue: Number(reason.current_value),
        dueValue: Number(reason.due_value),
      };
    }, {
      message: "meter-reading outbox event was processed by the real worker",
      timeout: 20_000,
    }).toEqual({ status: "Overdue", currentValue: 1200, dueValue: 1000 });

    const duePlan = record(workerPlan);
    const dueReason = record((duePlan.due_reasons as unknown[])[0]);
    expect(Number(dueReason.current_value)).toBe(1200);
    expect(Number(dueReason.due_value)).toBe(1000);

    const meterAuditResponse = await page.context().request.get(
      `/api/v1/audit-events/?resource_type=MeterReading&resource_id=${dueReadingId}`,
    );
    expect(meterAuditResponse.status(), await meterAuditResponse.text()).toBe(200);
    expect(record(await meterAuditResponse.json()).events).toEqual(expect.arrayContaining([
      expect.objectContaining({
        action: "meter.reading_accepted",
        actor: e2eUser.username,
        source: "manual",
        context: expect.objectContaining({ asset_id: assetId, meter_id: meterId }),
      }),
    ]));

    await expect.poll(async () => {
      const response = await page.context().request.get("/api/v1/notifications/");
      if (response.status() !== 200) return false;
      const payload = record(await response.json());
      return (payload.notifications as unknown[]).some((item) => {
        const notification = record(item);
        return notification.resource_type === "MaintenancePlan" && notification.resource_id === planId;
      });
    }, {
      message: "worker emitted and processed the maintenance.due outbox event",
      timeout: 20_000,
    }).toBe(true);

    await openNavigation(page, "Schedule", "Schedule");
    const overdueRow = planRow(page, planName);
    await expect(overdueRow).toContainText("Overdue");

    const workPayload = await mutate(page, "POST", `/api/v1/maintenance/plans/${planId}/create-work-order/`, () =>
      overdueRow.getByRole("button", { name: "Create planned work", exact: true }).click(),
    );
    const createdWork = record(workPayload.work_order);
    const workOrderId = String(createdWork.id);
    const workOrderNumber = String(createdWork.number);
    expect(record(createdWork.service_package_snapshot).version).toBe(1);

    await page.getByRole("tab", { name: "Service packages", exact: true }).click();
    await page.getByLabel(/^Package name\b/).fill(packageName);
    await page.getByLabel(/^Description\b/).fill("Version two applies only to future work");
    await page.getByLabel(/^Required task\b/).fill(replacementTask);
    const replacementPayload = await mutate(page, "POST", "/api/v1/maintenance/service-packages/", () =>
      page.getByRole("button", { name: "Create package", exact: true }).click(),
    );
    expect(record(replacementPayload.service_package).version).toBe(2);

    await openNavigation(page, "Work", "Work orders");
    await page.getByRole("link", { name: workOrderNumber, exact: true }).click();
    const assignmentForm = page.locator('form[aria-label="Work-order assignment"]');
    await assignmentForm.getByRole("checkbox", { name: "Taylor Technician · technician@example.com", exact: true }).check();
    await assignmentForm.getByLabel(/^Team lead/).selectOption({ label: "Taylor Technician · technician@example.com" });
    await mutate(page, "POST", `/api/v1/maintenance/work-orders/${workOrderId}/assignments/`, () =>
      assignmentForm.getByRole("button", { name: "Save assigned team", exact: true }).click(),
    );
    await mutate(page, "POST", `/api/v1/maintenance/work-orders/${workOrderId}/transition/`, () =>
      page.getByRole("button", { name: "Ready for work", exact: true }).click(),
    );
    await expect(page.getByText("Ready", { exact: true }).first()).toBeVisible();

    await logout(page);
    await loginAs(page, "technician@example.com");
    await openNavigation(page, "My Work", "My work");
    await page.getByRole("link", { name: workOrderNumber, exact: true }).click();
    await mutate(page, "POST", `/api/v1/maintenance/work-orders/${workOrderId}/transition/`, () =>
      page.getByRole("button", { name: "Start work", exact: true }).click(),
    );
    await expect(page.getByText("InProgress", { exact: true }).first()).toBeVisible();

    const taskRow = page.getByText(taskName, { exact: true }).locator("..").locator("..");
    const syncPayload = await mutate(page, "POST", "/api/v1/offline/sync/", () =>
      taskRow.getByRole("button", { name: "Complete task", exact: true }).click(),
    );
    expect(record((syncPayload.results as unknown[])[0]).status).toBe("synced");
    await expect(page.getByText("Synchronized", { exact: true }).first()).toBeVisible();
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.getByText(taskName, { exact: true }).locator("..").locator("..").getByRole("button", { name: "Complete task", exact: true })).toHaveCount(0);

    await openNavigation(page, "Assets", "Assets");
    await page.getByRole("link", { name: unit, exact: true }).click();
    await page.getByRole("combobox", { name: "Existing meter", exact: true }).selectOption(meterId);
    await page.getByLabel(/^Reading\b/).fill("1250");
    const completionReadingPayload = await mutate(page, "POST", `/api/v1/assets/${assetId}/meters/`, () =>
      page.getByRole("button", { name: "Record meter", exact: true }).click(),
    );
    const completionReadingId = String(record(completionReadingPayload.reading).id);

    await openNavigation(page, "My Work", "My work");
    await page.getByRole("link", { name: workOrderNumber, exact: true }).click();
    await page.getByLabel(/^Completion summary\b/).fill(completionSummary);
    await page.getByRole("combobox", { name: "Completion meter", exact: true }).selectOption(completionReadingId);
    const patchPromise = page.waitForResponse((response) =>
      new URL(response.url()).pathname === `/api/v1/maintenance/work-orders/${workOrderId}/`
      && response.request().method() === "PATCH",
    );
    const completePromise = page.waitForResponse((response) =>
      new URL(response.url()).pathname === `/api/v1/maintenance/work-orders/${workOrderId}/transition/`
      && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Complete work", exact: true }).click();
    for (const response of await Promise.all([patchPromise, completePromise])) {
      expect(response.status(), await response.text()).toBeLessThan(300);
    }
    await expect(page.getByText("Completed", { exact: true }).first()).toBeVisible();

    await logout(page);
    await loginAs(page, e2eUser.username);
    await openNavigation(page, "Work", "Work orders");
    await page.getByRole("link", { name: workOrderNumber, exact: true }).click();
    await mutate(page, "POST", `/api/v1/maintenance/work-orders/${workOrderId}/transition/`, () =>
      page.getByRole("button", { name: "Verify and close", exact: true }).click(),
    );
    await expect(page.getByText("Closed", { exact: true }).first()).toBeVisible();

    await openNavigation(page, "Schedule", "Schedule");
    const resetRow = planRow(page, planName);
    await expect(resetRow).toContainText("Current");
    await expect(resetRow).toContainText(/next due 2250(?:\.0+)?/);

    const workResponse = await page.context().request.get(`/api/v1/maintenance/work-orders/${workOrderId}/`);
    expect(workResponse.status(), await workResponse.text()).toBe(200);
    const work = record(record(await workResponse.json()).work_order);
    expect(work.status).toBe("Closed");
    expect(work.maintenance_plan_id).toBe(planId);
    expect(work.completion_meter_id).toBe(completionReadingId);
    expect(work.completion_summary).toBe(completionSummary);
    const snapshot = record(work.service_package_snapshot);
    expect(snapshot.name).toBe(packageName);
    expect(snapshot.version).toBe(1);
    expect(record((snapshot.tasks as unknown[])[0]).title).toBe(taskName);

    const finalPlanResponse = await page.context().request.get(`/api/v1/maintenance/plans/?asset_id=${assetId}`);
    expect(finalPlanResponse.status(), await finalPlanResponse.text()).toBe(200);
    const finalPlan = record((record(await finalPlanResponse.json()).plans as unknown[]).find((item) => record(item).id === planId));
    const finalTrigger = record((finalPlan.triggers as unknown[])[0]);
    const finalReason = record((finalPlan.due_reasons as unknown[])[0]);
    expect(finalPlan.due_status).toBe("Current");
    expect(Number(finalTrigger.last_completed_value)).toBe(1250);
    expect(Number(finalReason.current_value)).toBe(1250);
    expect(Number(finalReason.due_value)).toBe(2250);

    const packageResponse = await page.context().request.get("/api/v1/maintenance/service-packages/");
    expect(packageResponse.status(), await packageResponse.text()).toBe(200);
    const versions = (record(await packageResponse.json()).service_packages as unknown[])
      .map(record)
      .filter((item) => item.name === packageName);
    expect(versions.map((item) => item.version).sort()).toEqual([1, 2]);

    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});
