import { createHash } from "node:crypto";

import { expect, test, type APIResponse, type BrowserContext, type Locator, type Page } from "@playwright/test";

import { collectBrowserDiagnostics, e2eUser } from "./helpers";

type Json = Record<string, unknown>;

const baseURL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:8088";
const runTag = (process.env.E2E_RUN_ID ?? "manual").replace(/[^a-zA-Z0-9]/g, "").slice(-16) || "manual";
const partNumber = `E2E-COUNT-${runTag}`.toUpperCase();
const reason = `E2E count approval ${runTag}`;

function operationKey(label: string): string {
  const value = createHash("sha256").update(`${runTag}:${label}`).digest("hex");
  return `${value.slice(0, 8)}-${value.slice(8, 12)}-4${value.slice(13, 16)}-8${value.slice(17, 20)}-${value.slice(20, 32)}`;
}

function record(value: unknown, label: string): Json {
  expect(value, `${label} must be an object`).toBeTruthy();
  expect(typeof value, `${label} must be an object`).toBe("object");
  expect(Array.isArray(value), `${label} must be an object`).toBe(false);
  return value as Json;
}

function rows(value: unknown, label: string): Json[] {
  expect(Array.isArray(value), `${label} must be an array`).toBe(true);
  return value as Json[];
}

async function loginAs(page: Page, username: string): Promise<void> {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.getByLabel(/^Username/).fill(username);
  await page.getByLabel(/^Password/).fill(e2eUser.password);
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/login/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBe(200);
  await expect(page.getByRole("button", { name: "Sign out", exact: true })).toBeVisible();
}

async function getJson(page: Page, path: string): Promise<Json> {
  const response = await page.context().request.get(path);
  expect(response.status(), await response.text()).toBe(200);
  return record(await response.json(), path);
}

async function postJson(page: Page, path: string, data: Json, idempotencyKey: string): Promise<APIResponse> {
  const csrfResponse = await page.context().request.get("/api/v1/auth/csrf/");
  expect(csrfResponse.status(), await csrfResponse.text()).toBe(200);
  const csrf = record(await csrfResponse.json(), "CSRF response");
  return page.context().request.post(path, {
    data,
    headers: {
      Accept: "application/json",
      "Idempotency-Key": idempotencyKey,
      "X-CSRFToken": String(csrf.csrf_token ?? ""),
    },
  });
}

function panel(page: Page, title: string): Locator {
  return page.getByRole("heading", { level: 2, name: title, exact: true }).locator("..").locator("..");
}

function countRow(page: Page): Locator {
  return panel(page, "Inventory counts").locator("article.workflow-row").filter({ hasText: reason });
}

function stockRow(page: Page): Locator {
  return panel(page, "Stock on hand").locator("tbody tr").filter({ hasText: partNumber });
}

async function expectStock(page: Page, onHand: string): Promise<void> {
  const row = stockRow(page);
  await expect(row).toHaveCount(1);
  await expect(row.locator("td").nth(2)).toHaveText(onHand);
  await expect(row.locator("td").nth(3)).toHaveText("0.000");
  await expect(row.locator("td").nth(4)).toHaveText(onHand);
}

async function closeContexts(contexts: BrowserContext[]): Promise<void> {
  await Promise.all(contexts.map((context) => context.close()));
}

test("@inventory count variance waits for independent approval and posts exactly once", async ({ browser }, testInfo) => {
  test.setTimeout(90_000);
  const contexts = await Promise.all([
    browser.newContext({ baseURL }),
    browser.newContext({ baseURL }),
    browser.newContext({ baseURL }),
  ]);
  const [clerkPage, managerPage, auditorPage] = await Promise.all(contexts.map((context) => context.newPage()));
  const diagnostics = [clerkPage, managerPage, auditorPage].map(collectBrowserDiagnostics);

  try {
    await Promise.all([
      loginAs(clerkPage, "parts.clerk@example.com"),
      loginAs(managerPage, "purchasing.approver@example.com"),
      loginAs(auditorPage, "fleet.manager@example.com"),
    ]);

    const bins = rows((await getJson(clerkPage, "/api/v1/inventory/bins/")).bins, "bins");
    const stockBin = bins.find((row) => row.warehouse_code === "MAIN" && row.code === "A-01");
    expect(stockBin, "seed MAIN/A-01 bin").toBeTruthy();
    const binId = String(stockBin!.id);

    const partResponse = await postJson(
      managerPage,
      "/api/v1/inventory/parts/",
      {
        number: partNumber,
        name: `Count approval part ${runTag}`,
        unit_of_measure: "each",
        default_unit_cost: "10.00",
      },
      operationKey("part"),
    );
    expect(partResponse.status(), await partResponse.text()).toBe(201);
    const part = record(record(await partResponse.json(), "part response").part, "part");
    const partId = String(part.id);

    const openingResponse = await postJson(
      managerPage,
      "/api/v1/inventory/adjustments/",
      {
        part_id: partId,
        bin_id: binId,
        quantity: "5",
        reason: `E2E opening balance ${runTag}`,
      },
      operationKey("opening-balance"),
    );
    expect(openingResponse.status(), await openingResponse.text()).toBe(201);
    await Promise.all([clerkPage.reload({ waitUntil: "domcontentloaded" }), managerPage.reload({ waitUntil: "domcontentloaded" })]);

    await clerkPage.goto("/inventory");
    await expect(clerkPage.getByRole("heading", { level: 1, name: "Inventory", exact: true })).toBeVisible();
    await expectStock(clerkPage, "5.000");
    await clerkPage.getByRole("tab", { name: "Count stock", exact: true }).click();
    await clerkPage.getByRole("combobox", { name: "Part", exact: true }).selectOption(partId);
    await clerkPage.getByRole("combobox", { name: "Bin", exact: true }).selectOption(binId);
    await clerkPage.getByLabel(/^Counted quantity\b/).fill("7");
    await clerkPage.getByLabel(/^Reason\b/).fill(reason);
    const countResponsePromise = clerkPage.waitForResponse(
      (response) => response.url().endsWith("/api/v1/inventory/counts/") && response.request().method() === "POST",
    );
    await clerkPage.getByRole("button", { name: "Count stock", exact: true }).click();
    const countResponse = await countResponsePromise;
    expect(countResponse.status(), await countResponse.text()).toBe(201);
    const count = record(record(await countResponse.json(), "count response").count, "count");
    const countId = String(count.id);
    const countOperationId = String(count.operation_id);
    expect(count).toMatchObject({
      status: "PendingApproval",
      approval_required: true,
      approved_by_id: null,
      posted_by_id: null,
    });
    expect(count).not.toHaveProperty("total_variance_value");
    await expect(
      clerkPage.getByText("Inventory count is pending approval. Stock has not changed.", { exact: true }),
    ).toBeVisible();
    await expect(countRow(clerkPage).getByText("PendingApproval", { exact: true })).toBeVisible();
    await expect(countRow(clerkPage)).not.toContainText("variance value");
    await expect(countRow(clerkPage).getByRole("button", { name: "Approve inventory count" })).toHaveCount(0);
    await expectStock(clerkPage, "5.000");

    const pendingHistory = rows(
      (await getJson(clerkPage, `/api/v1/inventory/parts/${partId}/history/`)).transactions,
      "pending part transactions",
    );
    expect(pendingHistory.filter((transaction) => transaction.type === "COUNT_ADJUSTMENT")).toHaveLength(0);

    const selfApproval = await postJson(
      clerkPage,
      `/api/v1/inventory/counts/${countId}/approve/`,
      {},
      operationKey("self-approval"),
    );
    expect(selfApproval.status()).toBe(403);
    expect(await selfApproval.json()).toMatchObject({ error: { code: "permission_denied" } });
    const directAdjustment = await postJson(
      clerkPage,
      "/api/v1/inventory/adjustments/",
      { part_id: partId, bin_id: binId, quantity: "100", reason: "Unauthorized direct mutation" },
      operationKey("unauthorized-adjustment"),
    );
    expect(directAdjustment.status()).toBe(403);
    expect(await directAdjustment.json()).toMatchObject({ error: { code: "permission_denied" } });

    const countHeaders = await countResponse.request().allHeaders();
    const countRetry = await postJson(
      clerkPage,
      "/api/v1/inventory/counts/",
      {
        warehouse_id: String(stockBin!.warehouse_id),
        reason,
        lines: [{ part_id: partId, bin_id: binId, counted_quantity: "7" }],
      },
      String(countHeaders["idempotency-key"]),
    );
    expect(countRetry.status(), await countRetry.text()).toBe(201);
    expect(record(record(await countRetry.json(), "count retry response").count, "retried count").id).toBe(countId);
    const countRows = rows((await getJson(clerkPage, "/api/v1/inventory/counts/")).counts, "counts");
    expect(countRows.filter((row) => row.reason === reason)).toHaveLength(1);
    await expectStock(clerkPage, "5.000");

    await managerPage.goto("/inventory");
    await expect(managerPage.getByRole("heading", { level: 1, name: "Inventory", exact: true })).toBeVisible();
    await expect(countRow(managerPage).getByText("PendingApproval", { exact: true })).toBeVisible();
    const approvalResponsePromise = managerPage.waitForResponse(
      (response) => response.url().endsWith(`/api/v1/inventory/counts/${countId}/approve/`) && response.request().method() === "POST",
    );
    await countRow(managerPage).getByRole("button", { name: "Approve inventory count", exact: true }).click();
    const approvalResponse = await approvalResponsePromise;
    expect(approvalResponse.status(), await approvalResponse.text()).toBe(200);
    const approvedCount = record(record(await approvalResponse.json(), "approval response").count, "approved count");
    expect(approvedCount).toMatchObject({ id: countId, status: "Posted", approval_required: true });
    expect(approvedCount.approved_by_id).toBeTruthy();
    expect(approvedCount.posted_by_id).toBe(approvedCount.approved_by_id);
    await expect(managerPage.getByText("Inventory count approved and posted.", { exact: true })).toBeVisible();
    await expect(countRow(managerPage).getByText("Posted", { exact: true })).toBeVisible();
    await expect(countRow(managerPage).getByRole("button", { name: "Approve inventory count" })).toHaveCount(0);
    await expectStock(managerPage, "7.000");

    const approvalHeaders = await approvalResponse.request().allHeaders();
    const approvalOperationId = String(approvalHeaders["idempotency-key"]);
    const approvalRetry = await postJson(
      managerPage,
      `/api/v1/inventory/counts/${countId}/approve/`,
      {},
      approvalOperationId,
    );
    expect(approvalRetry.status(), await approvalRetry.text()).toBe(200);
    expect(record(record(await approvalRetry.json(), "approval retry response").count, "retried approval").id).toBe(countId);

    await managerPage.getByRole("combobox", { name: "History bin", exact: true }).selectOption(binId);
    const countTransactionRow = panel(managerPage, "Bin transaction history")
      .locator("tbody tr")
      .filter({ hasText: partNumber })
      .filter({ hasText: /count adjustment/i });
    await expect(countTransactionRow).toHaveCount(1);
    await expect(countTransactionRow.locator("td").nth(3)).toHaveText("2.000");
    await expect(countTransactionRow).toContainText(reason);

    await managerPage.reload({ waitUntil: "domcontentloaded" });
    await expect(countRow(managerPage).getByText("Posted", { exact: true })).toBeVisible();
    await expectStock(managerPage, "7.000");
    await managerPage.getByRole("combobox", { name: "History bin", exact: true }).selectOption(binId);
    await expect(
      panel(managerPage, "Bin transaction history")
        .locator("tbody tr")
        .filter({ hasText: partNumber })
        .filter({ hasText: /count adjustment/i }),
    ).toHaveCount(1);

    const durableHistory = rows(
      (await getJson(managerPage, `/api/v1/inventory/parts/${partId}/history/`)).transactions,
      "durable part transactions",
    );
    const countTransactions = durableHistory.filter(
      (transaction) => transaction.type === "COUNT_ADJUSTMENT" && transaction.reference_id === countId,
    );
    expect(countTransactions).toHaveLength(1);
    expect(countTransactions[0]).toMatchObject({
      quantity: "2.000",
      unit_cost: "10.0000",
      total_cost: "20.0000",
      reason,
    });
    const durableStock = rows(
      (await getJson(managerPage, `/api/v1/inventory/stock/?part_id=${partId}&bin_id=${binId}`)).stock,
      "durable stock",
    );
    expect(durableStock).toEqual([
      expect.objectContaining({
        part_id: partId,
        bin_id: binId,
        quantity_on_hand: "7.000",
        quantity_reserved: "0.000",
        available_quantity: "7.000",
      }),
    ]);

    await auditorPage.goto("/audit");
    await auditorPage.getByLabel("Resource type", { exact: true }).fill("InventoryCount");
    const visibleAuditRows = panel(auditorPage, "Append-only events").locator("tbody tr").filter({ hasText: countId });
    await expect(visibleAuditRows).toHaveCount(2);
    await expect(visibleAuditRows.filter({ hasText: "Draft → PendingApproval" })).toHaveCount(1);
    await expect(visibleAuditRows.filter({ hasText: "PendingApproval → Posted" })).toHaveCount(1);

    const auditEvents = rows(
      (await getJson(
        auditorPage,
        `/api/v1/audit-events/?resource_type=InventoryCount&resource_id=${countId}`,
      )).events,
      "count audit events",
    );
    expect(auditEvents).toHaveLength(2);
    expect(auditEvents.find((event) => event.action === "inventory_count.pending_approval")).toMatchObject({
      actor: "parts.clerk@example.com",
      previous_state: "Draft",
      new_state: "PendingApproval",
      correlation_id: countOperationId,
    });
    const postedAudit = record(
      auditEvents.find((event) => event.action === "inventory_count.posted"),
      "posted count audit",
    );
    expect(postedAudit).toMatchObject({
      actor: "purchasing.approver@example.com",
      previous_state: "PendingApproval",
      new_state: "Posted",
      correlation_id: countOperationId,
    });
    expect(record(postedAudit.context, "posted count audit context").approval_operation_id).toBe(approvalOperationId);

    diagnostics.forEach((entry) => entry.assertClean());
  } finally {
    await Promise.all(diagnostics.map((entry) => entry.attach(testInfo).catch(() => undefined)));
    await closeContexts(contexts);
  }
});
