import { expect, type Page, test } from "@playwright/test";

import { collectBrowserDiagnostics } from "./helpers";

type Json = Record<string, unknown>;

const password = process.env.E2E_PASSWORD ?? "DemoPass123!";
const defectDescription = "E2E brake vibration under light pedal pressure";
const workSummary = "E2E diagnose and repair front brake vibration";
const taskTitle = "Inspect and correct front brake vibration";
const laborNote = "Cleaned mating surfaces and torqued front wheel assemblies";
const workNote = "Road-test route reserved; no secondary defects observed.";
const completionSummary = "Brake vibration corrected and road test completed successfully.";
const outOfServiceReason = "Brake vibration requires shop inspection";
const returnToServiceReason = "Repair verified and road test passed";
const attachmentName = "brake-defect-evidence.txt";
const attachmentBody = "Fleetline E2E defect evidence\nasset=TRK-012\narea=Brakes\n";

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
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toHaveCount(0);
  await expect(page.locator("main")).toBeVisible();
}

async function logout(page: Page) {
  const responsePromise = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/auth/logout/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBe(200);
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
}

async function getJson(page: Page, path: string): Promise<Json> {
  const response = await page.context().request.get(path);
  expect(response.status(), await response.text()).toBe(200);
  return response.json() as Promise<Json>;
}

function event(
  events: Json[],
  resourceType: string,
  resourceId: string,
  action: string,
  previousState = "",
  newState = "",
): Json {
  const found = events.find(
    (candidate) =>
      candidate.resource_type === resourceType &&
      candidate.resource_id === resourceId &&
      candidate.action === action &&
      candidate.previous_state === previousState &&
      candidate.new_state === newState,
  );
  expect(
    found,
    `${resourceType} ${resourceId}: ${action} ${previousState} -> ${newState}`,
  ).toBeTruthy();
  expect(Number.isNaN(Date.parse(String(found?.occurred_at))), "audit timestamp must be ISO 8601").toBe(false);
  return found as Json;
}

test("@maintenance driver defect becomes a completed, audited repair with attachment, labor, part, and RTS", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  const diagnostics = collectBrowserDiagnostics(page);
  try {
    await loginAs(page, "driver@example.com");
    await expect(page.getByRole("heading", { level: 1, name: "Home", exact: true })).toBeVisible();
    await page.getByRole("link", { name: "Report a defect", exact: true }).first().click();
    await expect(page.getByRole("heading", { level: 1, name: "Report a defect", exact: true })).toBeVisible();

    const assetSelect = page.getByLabel(/^Asset/);
    const assetOption = assetSelect.locator("option").filter({ hasText: "TRK-012" });
    const assetId = await assetOption.getAttribute("value");
    expect(assetId, "deterministic driver-assigned asset").toBeTruthy();
    await assetSelect.selectOption(assetId!);
    await page.getByRole("button", { name: "Brakes", exact: true }).click();
    await page.getByRole("button", { name: "No", exact: true }).click();
    await page.getByLabel(/^Describe it/).fill(defectDescription);
    await page.getByLabel("Add photo or file", { exact: true }).setInputFiles({
      name: attachmentName,
      mimeType: "text/plain",
      buffer: Buffer.from(attachmentBody),
    });

    const defectSyncPromise = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/v1/offline/sync/") && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Submit defect", exact: true }).click();
    await expect(page.getByRole("status").filter({ hasText: "Saved on this device" })).toBeVisible();
    const defectSync = await defectSyncPromise;
    expect(defectSync.status(), await defectSync.text()).toBe(200);
    const syncResult = rows((await defectSync.json()) as Json, "results").find(
      (candidate) => candidate.status === "synced",
    );
    const defect = record(syncResult, "result");
    const defectId = String(defect.id);
    expect(defect).toMatchObject({
      asset_id: assetId,
      category: "Brakes",
      description: defectDescription,
      status: "Open",
    });
    await expect(page.getByTestId("sync-status").first()).toContainText("Synchronized");

    const attachments = rows(
      await getJson(
        page,
        `/api/v1/attachments/?resource_type=Defect&resource_id=${encodeURIComponent(defectId)}`,
      ),
      "attachments",
    );
    expect(attachments).toHaveLength(1);
    expect(attachments[0]).toMatchObject({
      name: attachmentName,
      content_type: "text/plain",
      size: Buffer.byteLength(attachmentBody),
    });
    const attachmentId = String(attachments[0].id);
    const download = await page.context().request.get(`/api/v1/attachments/${attachmentId}/download/`);
    expect(download.status(), await download.text()).toBe(200);
    expect((await download.body()).equals(Buffer.from(attachmentBody))).toBe(true);
    await logout(page);

    await loginAs(page, "supervisor@example.com");
    const notificationsButton = page
      .locator("header.topbar")
      .getByRole("button", { name: /^Notifications/ });
    await notificationsButton.click();
    await expect(page.getByRole("region", { name: "Notifications", exact: true })).toBeVisible();
    await expect
      .poll(async () => {
        const payload = await getJson(page, "/api/v1/notifications/");
        return rows(payload, "notifications").some(
          (notification) =>
            notification.resource_type === "Defect" && notification.resource_id === defectId,
        );
      }, { message: "worker-created notification for this defect" })
      .toBe(true);
    await page.reload({ waitUntil: "domcontentloaded" });
    await notificationsButton.click();
    const notificationMenu = page.getByRole("region", { name: "Notifications", exact: true });
    const defectNotification = notificationMenu.locator("article").filter({ hasText: defectId });
    await expect(defectNotification).toHaveCount(1);
    await expect(defectNotification.getByText("Defect Created", { exact: true })).toBeVisible();
    await expect(defectNotification).toContainText("Review the linked operational record.");
    await expect(defectNotification).toContainText(`Defect ${defectId}`);
    await expect(defectNotification.getByText(/^Unread\b/)).toBeVisible();
    const markReadPromise = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/v1/notifications/") &&
        response.request().method() === "POST",
    );
    await defectNotification.getByRole("button", { name: "Mark as read", exact: true }).click();
    const markReadResponse = await markReadPromise;
    expect(markReadResponse.status(), await markReadResponse.text()).toBe(200);
    await expect(defectNotification.getByText(/^Read\b/)).toBeVisible();
    await expect(defectNotification.getByRole("button", { name: "Mark as read" })).toHaveCount(0);

    await page.goto("/defects");
    await expect(page.getByRole("heading", { level: 1, name: "Triage defects", exact: true })).toBeVisible();
    const defectRow = page.locator("article.workflow-row").filter({ hasText: defectDescription });
    await expect(defectRow).toHaveCount(1);
    const acknowledgePromise = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/maintenance/defects/${defectId}/transition/`) &&
        response.request().method() === "POST",
    );
    await defectRow.getByRole("button", { name: "Acknowledge", exact: true }).click();
    expect((await acknowledgePromise).status()).toBe(200);
    await expect(defectRow.getByText("Acknowledged", { exact: true })).toBeVisible();

    const requestPromise = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/maintenance/defects/${defectId}/request/`) &&
        response.request().method() === "POST",
    );
    await defectRow.getByRole("button", { name: "Create maintenance request", exact: true }).click();
    const requestResponse = await requestPromise;
    expect(requestResponse.status(), await requestResponse.text()).toBe(201);
    const maintenanceRequest = record(
      (await requestResponse.json()) as Json,
      "maintenance_request",
    );
    const requestId = String(maintenanceRequest.id);
    expect(maintenanceRequest).toMatchObject({ defect_id: defectId, asset_id: assetId, status: "Submitted" });

    await page.getByRole("tab", { name: "Maintenance requests", exact: true }).click();
    const requestRow = page.locator("article.workflow-row").filter({ hasText: defectDescription });
    await expect(requestRow).toHaveCount(1);
    await requestRow.getByRole("button", { name: "Approve request", exact: true }).click();
    await expect(requestRow.getByText("Approved", { exact: true })).toBeVisible();
    await requestRow.getByRole("button", { name: "Create work order", exact: true }).click();

    const createPanel = panel(page, "Create work order");
    await createPanel.getByLabel(/^Summary/).fill(workSummary);
    await createPanel
      .getByLabel(/^Assigned technician/)
      .selectOption({ label: "Taylor Technician · technician@example.com" });
    await createPanel.getByLabel(/^Priority/).selectOption("high");
    const workOrderPromise = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/maintenance/requests/${requestId}/work-order/`) &&
        response.request().method() === "POST",
    );
    const readyPromise = page.waitForResponse(
      (response) =>
        response.url().includes("/api/v1/maintenance/work-orders/") &&
        response.url().endsWith("/transition/") &&
        response.request().method() === "POST",
    );
    await createPanel.getByRole("button", { name: "Create work order", exact: true }).click();
    const workOrderResponse = await workOrderPromise;
    expect(workOrderResponse.status(), await workOrderResponse.text()).toBe(201);
    const workOrder = record((await workOrderResponse.json()) as Json, "work_order");
    const workOrderId = String(workOrder.id);
    const workOrderNumber = String(workOrder.number);
    expect(workOrder).toMatchObject({
      request_id: requestId,
      asset_id: assetId,
      assigned_to: "Taylor Technician",
      summary: workSummary,
      status: "Draft",
    });
    expect((await readyPromise).status()).toBe(200);
    await expect(createPanel).toHaveCount(0);

    await page.goto(`/work-orders/${workOrderId}/`);
    const taskPanel = panel(page, "Add work-order task");
    await taskPanel.getByLabel(/^Task title\b/).fill(taskTitle);
    await taskPanel.getByLabel("Task instructions", { exact: true })
      .fill("Inspect rotors, hubs, calipers, and wheel fastener torque.");
    await taskPanel.getByLabel(/^Sequence\b/).fill("1");
    await expect(taskPanel.getByRole("checkbox", { name: "Required task", exact: true })).toBeChecked();
    const taskPromise = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/maintenance/work-orders/${workOrderId}/tasks/`) &&
        response.request().method() === "POST",
    );
    await taskPanel.getByRole("button", { name: "Add task", exact: true }).click();
    const taskResponse = await taskPromise;
    expect(taskResponse.status(), await taskResponse.text()).toBe(201);
    const task = record((await taskResponse.json()) as Json, "task");
    const taskId = String(task.id);
    await expect(page.getByText("Work-order task added.", { exact: true })).toBeVisible();
    await expect(panel(page, "Tasks").getByText(taskTitle, { exact: true })).toBeVisible();

    await page.goto(`/assets/${assetId}/`);
    await expect(page.getByRole("heading", { level: 1, name: "TRK-012", exact: true })).toBeVisible();
    page.once("dialog", (dialog) => void dialog.accept(outOfServiceReason));
    const outOfServicePromise = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/assets/${assetId}/availability/`) &&
        response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Place out of service", exact: true }).click();
    expect((await outOfServicePromise).status()).toBe(200);
    await expect(page.getByText("OutOfService", { exact: true }).first()).toBeVisible();
    await logout(page);

    await loginAs(page, "technician@example.com");
    await page.getByRole("link", { name: "My Work", exact: true }).first().click();
    await expect(page.getByRole("heading", { level: 1, name: "My work", exact: true })).toBeVisible();
    const workLink = page.getByRole("link", { name: workOrderNumber, exact: true });
    await expect(workLink).toBeVisible();
    await workLink.click();
    await expect(page.getByRole("heading", { level: 1, name: new RegExp(workOrderNumber) })).toBeVisible();

    const startPromise = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/maintenance/work-orders/${workOrderId}/transition/`) &&
        response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Start work", exact: true }).click();
    expect((await startPromise).status()).toBe(200);
    await expect(page.getByText("InProgress", { exact: true }).first()).toBeVisible();

    const taskRow = page.locator(".task-row").filter({ hasText: taskTitle });
    await expect(taskRow).toHaveCount(1);
    const taskSyncPromise = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/v1/offline/sync/") && response.request().method() === "POST",
    );
    await taskRow.getByRole("button", { name: "Complete task", exact: true }).click();
    const taskSync = await taskSyncPromise;
    expect(taskSync.status(), await taskSync.text()).toBe(200);
    expect(rows((await taskSync.json()) as Json, "results")[0]).toMatchObject({ status: "synced" });
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator(".task-row").filter({ hasText: taskTitle })).toBeVisible();
    await expect(
      page.locator(".task-row").filter({ hasText: taskTitle }).getByRole("button", { name: "Complete task" }),
    ).toHaveCount(0);

    await page.getByLabel(/^Hours/).fill("1.5");
    await page.getByLabel(/^Work performed/).fill(laborNote);
    const laborPromise = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/maintenance/work-orders/${workOrderId}/labor/`) &&
        response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Add labor", exact: true }).click();
    const laborResponse = await laborPromise;
    expect(laborResponse.status(), await laborResponse.text()).toBe(201);
    const labor = record((await laborResponse.json()) as Json, "labor_entry");
    expect(labor).toMatchObject({ minutes: 90, note: laborNote, technician: "Taylor Technician" });

    await page.getByLabel(/^Note/).fill(workNote);
    const noteSyncPromise = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/v1/offline/sync/") && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Save work note", exact: true }).click();
    const noteSync = await noteSyncPromise;
    expect(noteSync.status(), await noteSync.text()).toBe(200);
    const noteResult = rows((await noteSync.json()) as Json, "results")[0];
    expect(noteResult).toMatchObject({ status: "synced", result: { body: workNote, work_order_id: workOrderId } });
    await expect(page.getByTestId("sync-status").first()).toContainText("Synchronized");

    const parts = rows(await getJson(page, "/api/v1/inventory/parts/"), "parts");
    const oil = parts.find((candidate) => candidate.number === "OIL-15W40");
    expect(oil, "deterministic stocked repair part").toBeTruthy();
    const bins = rows(await getJson(page, "/api/v1/inventory/bins/"), "bins");
    const repairBin = bins.find(
      (candidate) => candidate.warehouse_code === "MAIN" && candidate.code === "A-01",
    );
    expect(repairBin, "deterministic MAIN/A-01 repair bin").toBeTruthy();

    await page.getByRole("link", { name: "Reserve or issue parts", exact: true }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Inventory", exact: true })).toBeVisible();
    await page.getByRole("tab", { name: "Issue part", exact: true }).click();
    await page.getByRole("combobox", { name: "Part", exact: true }).selectOption(String(oil!.id));
    await page.getByRole("combobox", { name: "Bin", exact: true }).selectOption(String(repairBin!.id));
    await expect(page.getByLabel(/^Work order ID/)).toHaveValue(workOrderId);
    await page.getByLabel(/^Quantity/).fill("2");
    const issuePromise = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/v1/inventory/issues/") && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Issue part", exact: true }).click();
    const issueResponse = await issuePromise;
    expect(issueResponse.status(), await issueResponse.text()).toBe(201);
    const issue = record((await issueResponse.json()) as Json, "transaction");
    expect(issue).toMatchObject({
      type: "ISSUE",
      part_id: String(oil!.id),
      bin_id: String(repairBin!.id),
      work_order_id: workOrderId,
      quantity: "-2.000",
    });
    expect(issue).not.toHaveProperty("total_cost");
    await expect(page.getByText("Issue transaction recorded.", { exact: true })).toBeVisible();

    await page.goto(`/work-orders/${workOrderId}/`);
    await page.getByLabel(/^Completion summary/).fill(completionSummary);
    const completedPromise = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/maintenance/work-orders/${workOrderId}/transition/`) &&
        response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Complete work", exact: true }).click();
    expect((await completedPromise).status()).toBe(200);
    await expect(page.getByText("Completed", { exact: true }).first()).toBeVisible();
    await logout(page);

    await loginAs(page, process.env.E2E_USERNAME ?? "fleet.manager@example.com");
    await page.goto(`/work-orders/${workOrderId}/`);
    await expect(page.getByText("Completed", { exact: true }).first()).toBeVisible();
    const closePromise = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/maintenance/work-orders/${workOrderId}/transition/`) &&
        response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Verify and close", exact: true }).click();
    expect((await closePromise).status()).toBe(200);
    await expect(page.getByText("Closed", { exact: true }).first()).toBeVisible();

    await page.goto(`/assets/${assetId}/`);
    await expect(page.getByText("OutOfService", { exact: true }).first()).toBeVisible();
    page.once("dialog", (dialog) => void dialog.accept(returnToServiceReason));
    const returnPromise = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/api/v1/assets/${assetId}/availability/`) &&
        response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Return to service", exact: true }).click();
    expect((await returnPromise).status()).toBe(200);
    await expect(page.getByText("Available", { exact: true }).first()).toBeVisible();

    const assetHistory = panel(page, "Complete asset history");
    await expect(assetHistory).toContainText(defectDescription);
    await expect(assetHistory).toContainText(workOrderNumber);
    await expect(assetHistory).toContainText(workSummary);
    await expect(assetHistory).toContainText(outOfServiceReason);
    await expect(assetHistory).toContainText(returnToServiceReason);

    const durableWorkOrder = record(
      await getJson(page, `/api/v1/maintenance/work-orders/${workOrderId}/`),
      "work_order",
    );
    expect(durableWorkOrder).toMatchObject({
      id: workOrderId,
      request_id: requestId,
      asset_id: assetId,
      status: "Closed",
      completion_summary: completionSummary,
      part_cost: "12.5000",
    });
    expect(rows({ tasks: durableWorkOrder.tasks }, "tasks")).toEqual([
      expect.objectContaining({ id: taskId, title: taskTitle, status: "Completed" }),
    ]);
    expect(rows({ labor_entries: durableWorkOrder.labor_entries }, "labor_entries")).toEqual([
      expect.objectContaining({ minutes: 90, note: laborNote, technician: "Taylor Technician" }),
    ]);
    expect(rows({ stock_transactions: durableWorkOrder.stock_transactions }, "stock_transactions")).toEqual([
      expect.objectContaining({ id: issue.id, work_order_id: workOrderId, type: "ISSUE" }),
    ]);

    const comments = rows(
      await getJson(
        page,
        `/api/v1/comments/?resource_type=WorkOrder&resource_id=${encodeURIComponent(workOrderId)}`,
      ),
      "comments",
    );
    expect(comments).toEqual([
      expect.objectContaining({ body: workNote, author: "Taylor Technician" }),
    ]);
    expect(record(await getJson(page, `/api/v1/assets/${assetId}/`), "asset")).toMatchObject({
      id: assetId,
      status: "Available",
    });

    const auditEvents = rows(await getJson(page, "/api/v1/audit-events/"), "events");
    expect(event(auditEvents, "Defect", defectId, "defect.created", "", "Open")).toMatchObject({
      actor: "driver@example.com",
    });
    expect(
      event(auditEvents, "Defect", defectId, "defect.transitioned", "Open", "Acknowledged"),
    ).toMatchObject({ actor: "supervisor@example.com" });
    expect(
      event(
        auditEvents,
        "MaintenanceRequest",
        requestId,
        "maintenance_request.transitioned",
        "Submitted",
        "Triaged",
      ),
    ).toMatchObject({ actor: "supervisor@example.com" });
    expect(
      event(
        auditEvents,
        "MaintenanceRequest",
        requestId,
        "maintenance_request.transitioned",
        "Triaged",
        "Approved",
      ),
    ).toMatchObject({ actor: "supervisor@example.com" });
    expect(event(auditEvents, "WorkOrder", workOrderId, "work_order.created", "", "Draft")).toMatchObject({
      actor: "supervisor@example.com",
      context: { request_id: requestId },
    });
    expect(
      event(auditEvents, "WorkOrder", workOrderId, "work_order.transitioned", "Draft", "Ready"),
    ).toMatchObject({ actor: "supervisor@example.com" });
    expect(
      event(
        auditEvents,
        "WorkOrder",
        workOrderId,
        "work_order.transitioned",
        "Ready",
        "InProgress",
      ),
    ).toMatchObject({ actor: "technician@example.com" });
    expect(
      event(
        auditEvents,
        "WorkOrder",
        workOrderId,
        "work_order.transitioned",
        "InProgress",
        "Completed",
      ),
    ).toMatchObject({ actor: "technician@example.com" });
    expect(
      event(
        auditEvents,
        "WorkOrder",
        workOrderId,
        "work_order.transitioned",
        "Completed",
        "Closed",
      ),
    ).toMatchObject({ actor: process.env.E2E_USERNAME ?? "fleet.manager@example.com" });
    expect(
      event(
        auditEvents,
        "Asset",
        assetId!,
        "asset.availability_changed",
        "Available",
        "OutOfService",
      ),
    ).toMatchObject({ actor: "supervisor@example.com", context: { reason: outOfServiceReason } });
    expect(
      event(
        auditEvents,
        "Asset",
        assetId!,
        "asset.availability_changed",
        "OutOfService",
        "Available",
      ),
    ).toMatchObject({
      actor: process.env.E2E_USERNAME ?? "fleet.manager@example.com",
      context: { reason: returnToServiceReason },
    });
    expect(
      auditEvents.find(
        (candidate) =>
          candidate.action === "attachment.linked" &&
          candidate.resource_id === attachmentId &&
          (candidate.context as Json).target_id === defectId,
      ),
    ).toMatchObject({ actor: "driver@example.com", source: "offline_sync" });
    expect(
      auditEvents.find(
        (candidate) => candidate.action === "labor.recorded" && candidate.resource_id === labor.id,
      ),
    ).toMatchObject({ actor: "technician@example.com", context: { work_order_id: workOrderId } });
    expect(
      auditEvents.find(
        (candidate) => candidate.action === "stock.issued" && candidate.resource_id === issue.id,
      ),
    ).toMatchObject({ actor: "technician@example.com" });

    await page.goto("/audit");
    const filteredAuditPromise = page.waitForResponse(
      (response) => response.url().includes("/api/v1/audit-events/?resource_type=WorkOrder"),
    );
    await page.getByPlaceholder("All resources").fill("WorkOrder");
    expect((await filteredAuditPromise).status()).toBe(200);
    const visibleAudit = panel(page, "Append-only events");
    const workOrderAuditRows = visibleAudit.locator("tbody tr").filter({ hasText: workOrderId });
    await expect.poll(() => workOrderAuditRows.count()).toBeGreaterThanOrEqual(7);
    const visibleAuditText = (await workOrderAuditRows.allTextContents()).join("\n");
    expect(visibleAuditText).toContain("supervisor@example.com");
    expect(visibleAuditText).toContain("technician@example.com");
    expect(visibleAuditText).toContain(process.env.E2E_USERNAME ?? "fleet.manager@example.com");
    expect(visibleAuditText).toContain("Draft → Ready");
    expect(visibleAuditText).toContain("Ready → InProgress");
    expect(visibleAuditText).toContain("InProgress → Completed");
    expect(visibleAuditText).toContain("Completed → Closed");

    await logout(page);
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});
