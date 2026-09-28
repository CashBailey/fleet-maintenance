import { randomUUID } from "node:crypto";

import { expect, test, type BrowserContext, type Page } from "@playwright/test";

import { collectBrowserDiagnostics, e2eUser } from "./helpers";

type Json = Record<string, unknown>;

const baseURL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:8088";
const supervisorUsername = "supervisor@example.com";
const leadUsername = "technician@example.com";
const teammateUsername = "technician.two@example.com";
const leadLabel = "Taylor Technician · technician@example.com";
const teammateLabel = "Robin Technician · technician.two@example.com";

function record(value: unknown, label: string): Json {
  expect(value, `${label} must be an object`).not.toBeNull();
  expect(typeof value, `${label} must be an object`).toBe("object");
  expect(Array.isArray(value), `${label} must be an object`).toBe(false);
  return value as Json;
}

function rows(value: unknown, label: string): Json[] {
  expect(Array.isArray(value), `${label} must be an array`).toBe(true);
  return value as Json[];
}

function identifier(value: unknown, label: string): string {
  const result = String(record(value, label).id ?? "");
  expect(result, `${label}.id`).toMatch(/^[0-9a-f-]{36}$/i);
  return result;
}

function panel(page: Page, title: string) {
  return page.getByRole("heading", { level: 2, name: title, exact: true }).locator("..").locator("..");
}

async function loginAs(page: Page, username: string): Promise<void> {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
  await page.getByLabel(/^Username/).fill(username);
  await page.getByLabel(/^Password/).fill(e2eUser.password);
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/login/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBe(200);
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toHaveCount(0);
  await expect(page.locator("main")).toBeVisible();
}

async function csrf(page: Page): Promise<string> {
  const response = await page.context().request.get("/api/v1/auth/csrf/");
  expect(response.status(), await response.text()).toBe(200);
  return String(record(await response.json(), "CSRF response").csrf_token ?? "");
}

async function getJson(page: Page, path: string): Promise<Json> {
  const response = await page.context().request.get(path);
  expect(response.status(), await response.text()).toBe(200);
  return record(await response.json(), path);
}

async function postJson(
  page: Page,
  path: string,
  data: Json,
  expectedStatus: number,
): Promise<Json> {
  const response = await page.context().request.post(path, {
    headers: {
      "Idempotency-Key": randomUUID(),
      "X-CSRFToken": await csrf(page),
    },
    data,
  });
  expect(response.status(), await response.text()).toBe(expectedStatus);
  return record(await response.json(), path);
}

test("@maintenance a supervisor assigns a two-technician team and the teammate completes assigned work", async ({ browser, page }, testInfo) => {
  test.setTimeout(120_000);
  const supervisorDiagnostics = collectBrowserDiagnostics(page);
  let teammateContext: BrowserContext | undefined;
  let teammateDiagnostics: ReturnType<typeof collectBrowserDiagnostics> | undefined;
  const testData: Json = {};

  try {
    await loginAs(page, supervisorUsername);

    const [bootstrap, directory] = await Promise.all([
      getJson(page, "/api/v1/bootstrap/"),
      getJson(page, "/api/v1/users/?role=technician"),
    ]);
    const asset = rows(bootstrap.assets, "bootstrap assets").find((candidate) => candidate.unit_number === "TRK-012");
    const technicians = rows(directory.users, "technician directory");
    const lead = technicians.find((candidate) => candidate.username === leadUsername);
    const teammate = technicians.find((candidate) => candidate.username === teammateUsername);
    expect(asset, "deterministic TRK-012 asset").toBeDefined();
    expect(lead, `deterministic technician ${leadUsername}`).toBeDefined();
    expect(teammate, `deterministic technician ${teammateUsername}`).toBeDefined();
    const assetId = identifier(asset, "TRK-012 asset");
    const leadId = identifier(lead, "lead technician");
    const teammateId = identifier(teammate, "second technician");

    const suffix = randomUUID().slice(0, 8).toUpperCase();
    const taskTitle = `Verify team repair ${suffix}`;
    const assignmentReason = `Two-person E2E repair team ${suffix}`;
    const created = await postJson(page, "/api/v1/maintenance/work-orders/", {
      asset_id: assetId,
      summary: `E2E team assignment ${suffix}`,
      complaint: "Exercise the local multi-technician assignment workflow.",
      priority: "normal",
    }, 201);
    const workOrder = record(created.work_order, "created work order");
    const workOrderId = identifier(workOrder, "created work order");
    const workOrderNumber = String(workOrder.number ?? "");
    expect(workOrderNumber).toMatch(/^WO-/);
    expect(rows(workOrder.assignees, "initial work-order assignees")).toEqual([]);
    testData.work_order_id = workOrderId;
    testData.work_order_number = workOrderNumber;
    testData.task_title = taskTitle;

    await page.goto(`/work-orders/${workOrderId}/`, { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { level: 1, name: new RegExp(`^${workOrderNumber}\\b`) })).toBeVisible();
    await expect(panel(page, "Work details")).toContainText(/Assigned team\s*—/);

    const taskPanel = panel(page, "Add work-order task");
    await taskPanel.getByLabel(/^Task title\b/).fill(taskTitle);
    await taskPanel.getByLabel("Task instructions", { exact: true }).fill("Complete and document the shared repair task.");
    await taskPanel.getByLabel(/^Sequence\b/).fill("1");
    await expect(taskPanel.getByRole("checkbox", { name: "Required task", exact: true })).toBeChecked();
    const taskResponsePromise = page.waitForResponse(
      (response) => response.url().endsWith(`/api/v1/maintenance/work-orders/${workOrderId}/tasks/`)
        && response.request().method() === "POST",
    );
    await taskPanel.getByRole("button", { name: "Add task", exact: true }).click();
    const taskResponse = await taskResponsePromise;
    expect(taskResponse.status(), await taskResponse.text()).toBe(201);
    const task = record(record(await taskResponse.json(), "task response").task, "created task");
    const taskId = identifier(task, "created task");
    testData.task_id = taskId;
    await expect(page.getByRole("status").filter({ hasText: "Work-order task added." })).toBeVisible();

    const assignmentForm = page.getByRole("form", { name: "Work-order assignment", exact: true });
    const leadCheckbox = assignmentForm.getByRole("checkbox", { name: leadLabel, exact: true });
    const teammateCheckbox = assignmentForm.getByRole("checkbox", { name: teammateLabel, exact: true });
    await expect(leadCheckbox).toBeVisible();
    await expect(teammateCheckbox).toBeVisible();
    await leadCheckbox.check();
    await teammateCheckbox.check();
    await assignmentForm.getByLabel(/^Team lead/).selectOption({ label: leadLabel });
    await assignmentForm.getByLabel(/^Assignment note/).fill(assignmentReason);

    const assignmentRequestPromise = page.waitForRequest(
      (request) => request.url().endsWith(`/api/v1/maintenance/work-orders/${workOrderId}/assignments/`)
        && request.method() === "POST",
    );
    const assignmentResponsePromise = page.waitForResponse(
      (response) => response.url().endsWith(`/api/v1/maintenance/work-orders/${workOrderId}/assignments/`)
        && response.request().method() === "POST",
    );
    await assignmentForm.getByRole("button", { name: "Save assigned team", exact: true }).click();
    const [assignmentRequest, assignmentResponse] = await Promise.all([
      assignmentRequestPromise,
      assignmentResponsePromise,
    ]);
    expect(assignmentResponse.status(), await assignmentResponse.text()).toBe(200);
    const assignmentHeaders = assignmentRequest.headers();
    const assignmentKey = assignmentHeaders["idempotency-key"] ?? "";
    const assignmentPayload = record(assignmentRequest.postDataJSON(), "UI assignment payload");
    expect(assignmentKey).toMatch(/^[0-9a-f-]{36}$/i);
    expect(assignmentPayload.reason).toBe(assignmentReason);
    expect(rows(assignmentPayload.assignees, "UI assignment assignees")).toEqual(expect.arrayContaining([
      expect.objectContaining({ user_id: leadId, role: "lead" }),
      expect.objectContaining({ user_id: teammateId, role: "technician" }),
    ]));
    expect(rows(assignmentPayload.assignees, "UI assignment assignees")).toHaveLength(2);
    testData.assignment_idempotency_key = assignmentKey;
    await expect(page.getByRole("status").filter({ hasText: "Assigned work-order team saved." })).toBeVisible();
    await expect(panel(page, "Work details")).toContainText("Taylor Technician");
    await expect(panel(page, "Work details")).toContainText("Robin Technician");

    const replay = await page.context().request.post(
      `/api/v1/maintenance/work-orders/${workOrderId}/assignments/`,
      {
        headers: {
          "Idempotency-Key": assignmentKey,
          "X-CSRFToken": assignmentHeaders["x-csrftoken"] ?? await csrf(page),
        },
        data: assignmentPayload,
      },
    );
    expect(replay.status(), await replay.text()).toBe(200);
    const replayWorkOrder = record(record(await replay.json(), "assignment replay").work_order, "replayed work order");
    expect(replayWorkOrder.id).toBe(workOrderId);

    const assignedDetail = record(
      (await getJson(page, `/api/v1/maintenance/work-orders/${workOrderId}/`)).work_order,
      "assigned work order",
    );
    expect(rows(assignedDetail.assignees, "active team")).toEqual([
      expect.objectContaining({ user_id: leadId, display_name: "Taylor Technician", role: "lead" }),
      expect.objectContaining({ user_id: teammateId, display_name: "Robin Technician", role: "technician" }),
    ]);
    const assignmentHistory = rows(assignedDetail.assignment_history, "assignment history");
    expect(assignmentHistory).toHaveLength(2);
    expect(assignmentHistory).toEqual(expect.arrayContaining([
      expect.objectContaining({
        action: "assigned",
        user_id: leadId,
        role: "lead",
        reason: assignmentReason,
        assigned_by: "Sam Supervisor",
      }),
      expect.objectContaining({
        action: "assigned",
        user_id: teammateId,
        role: "technician",
        reason: assignmentReason,
        assigned_by: "Sam Supervisor",
      }),
    ]));
    expect(new Set(assignmentHistory.map((event) => String(event.id))).size).toBe(2);
    expect(assignmentHistory.map((event) => event.sequence)).toEqual([1, 2]);

    const assignmentEvents = rows(
      (await getJson(
        page,
        `/api/v1/audit-events/?resource_type=WorkOrder&resource_id=${workOrderId}`,
      )).events,
      "work-order audit events",
    ).filter((event) => event.action === "work_order.assignments_changed");
    expect(assignmentEvents).toHaveLength(1);
    const assignmentAudit = assignmentEvents[0];
    expect(assignmentAudit).toMatchObject({
      actor: supervisorUsername,
      correlation_id: assignmentKey,
      context: {
        reason: assignmentReason,
      },
    });
    const assignmentAuditContext = record(assignmentAudit.context, "assignment audit context");
    expect(rows(assignmentAuditContext.before, "assignment audit before")).toEqual([]);
    expect(rows(assignmentAuditContext.after, "assignment audit after")).toHaveLength(2);
    expect(rows(assignmentAuditContext.assignment_event_ids, "assignment audit event IDs")).toHaveLength(2);
    expect(Number.isNaN(Date.parse(String(assignmentAudit.occurred_at)))).toBe(false);

    const readyResponsePromise = page.waitForResponse(
      (response) => response.url().endsWith(`/api/v1/maintenance/work-orders/${workOrderId}/transition/`)
        && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Ready for work", exact: true }).click();
    const readyResponse = await readyResponsePromise;
    expect(readyResponse.status(), await readyResponse.text()).toBe(200);
    await expect(page.getByText("Ready", { exact: true }).first()).toBeVisible();

    teammateContext = await browser.newContext({ baseURL });
    const teammatePage = await teammateContext.newPage();
    teammateDiagnostics = collectBrowserDiagnostics(teammatePage);
    await loginAs(teammatePage, teammateUsername);
    await teammatePage.getByRole("link", { name: "My Work", exact: true }).first().click();
    await expect(teammatePage.getByRole("heading", { level: 1, name: "My work", exact: true })).toBeVisible();
    const assignedWorkLink = teammatePage.getByRole("link", { name: workOrderNumber, exact: true });
    await expect(assignedWorkLink).toBeVisible();
    await assignedWorkLink.click();
    await expect(teammatePage.getByRole("heading", { level: 1, name: new RegExp(`^${workOrderNumber}\\b`) })).toBeVisible();
    await expect(panel(teammatePage, "Work details")).toContainText("Taylor Technician");
    await expect(panel(teammatePage, "Work details")).toContainText("Robin Technician");

    const startResponsePromise = teammatePage.waitForResponse(
      (response) => response.url().endsWith(`/api/v1/maintenance/work-orders/${workOrderId}/transition/`)
        && response.request().method() === "POST",
    );
    await teammatePage.getByRole("button", { name: "Start work", exact: true }).click();
    const startResponse = await startResponsePromise;
    expect(startResponse.status(), await startResponse.text()).toBe(200);
    await expect(teammatePage.getByText("InProgress", { exact: true }).first()).toBeVisible();

    const taskRow = teammatePage.locator(".task-row").filter({ hasText: taskTitle });
    await expect(taskRow).toHaveCount(1);
    const taskSyncPromise = teammatePage.waitForResponse(
      (response) => response.url().endsWith("/api/v1/offline/sync/")
        && response.request().method() === "POST",
    );
    await taskRow.getByRole("button", { name: "Complete task", exact: true }).click();
    const taskSync = await taskSyncPromise;
    expect(taskSync.status(), await taskSync.text()).toBe(200);
    const taskSyncResult = rows(record(await taskSync.json(), "task sync response").results, "task sync results")[0];
    expect(taskSyncResult).toMatchObject({
      status: "synced",
      result: {
        id: taskId,
        status: "Completed",
        completed_by_id: teammateId,
      },
    });
    await teammatePage.reload({ waitUntil: "domcontentloaded" });
    const durableTaskRow = teammatePage.locator(".task-row").filter({ hasText: taskTitle });
    await expect(durableTaskRow).toContainText("Completed");
    await expect(durableTaskRow.getByRole("button", { name: "Complete task" })).toHaveCount(0);

    const durableWorkOrder = record(
      (await getJson(page, `/api/v1/maintenance/work-orders/${workOrderId}/`)).work_order,
      "durable work order",
    );
    expect(rows(durableWorkOrder.assignees, "durable active team")).toHaveLength(2);
    expect(rows(durableWorkOrder.assignment_history, "durable assignment history")).toHaveLength(2);
    expect(rows(durableWorkOrder.tasks, "durable tasks")).toEqual([
      expect.objectContaining({ id: taskId, status: "Completed", completed_by_id: teammateId }),
    ]);

    const taskEvents = rows(
      (await getJson(
        page,
        `/api/v1/audit-events/?resource_type=WorkOrderTask&resource_id=${taskId}`,
      )).events,
      "task audit events",
    ).filter((event) => event.action === "work_order_task.transitioned");
    expect(taskEvents).toHaveLength(1);
    expect(taskEvents[0]).toMatchObject({
      actor: teammateUsername,
      previous_state: "Pending",
      new_state: "Completed",
      context: { work_order_id: workOrderId },
    });
    expect(Number.isNaN(Date.parse(String(taskEvents[0].occurred_at)))).toBe(false);

    const finalWorkOrderEvents = rows(
      (await getJson(
        page,
        `/api/v1/audit-events/?resource_type=WorkOrder&resource_id=${workOrderId}`,
      )).events,
      "final work-order audit events",
    );
    expect(finalWorkOrderEvents.filter((event) => event.action === "work_order.assignments_changed")).toHaveLength(1);
    expect(finalWorkOrderEvents).toEqual(expect.arrayContaining([
      expect.objectContaining({
        action: "work_order.transitioned",
        actor: teammateUsername,
        previous_state: "Ready",
        new_state: "InProgress",
      }),
    ]));

    supervisorDiagnostics.assertClean();
    teammateDiagnostics.assertClean();
  } finally {
    await testInfo.attach("work-order-team-test-data", {
      body: Buffer.from(JSON.stringify(testData, null, 2)),
      contentType: "application/json",
    });
    await supervisorDiagnostics.attach(testInfo);
    if (teammateDiagnostics) await teammateDiagnostics.attach(testInfo);
    if (teammateContext) await teammateContext.close();
  }
});
