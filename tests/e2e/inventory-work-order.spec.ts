import { expect, type Page, test } from "@playwright/test";

import { collectBrowserDiagnostics } from "./helpers";

type Json = Record<string, unknown>;

const password = process.env.E2E_PASSWORD ?? "DemoPass123!";
const partNumber = "E2E-INV-WO-01";
const partName = "E2E service filter";
const alternateNumber = "E2E-ALT-INV-01";
const storageAreaCode = "E2E-PARTS-CAGE";
const physicalAddress = "CAB-02/SHELF-03/DRAWER-01/BIN-04";

async function loginAs(page: Page, username: string) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
  await page.getByLabel(/^Username/).fill(username);
  await page.getByLabel(/^Password/).fill(password);
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/login/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBe(200);
  await expect(page.locator("main")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toHaveCount(0);
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

async function submit(page: Page, endpoint: string, button: string): Promise<Json> {
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith(endpoint) && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: button, exact: true }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBe(201);
  return response.json() as Promise<Json>;
}

async function getJson(page: Page, path: string): Promise<Json> {
  const response = await page.context().request.get(path);
  expect(response.status(), await response.text()).toBe(200);
  return response.json() as Promise<Json>;
}

function rows(payload: Json, key: string): Json[] {
  const value = payload[key];
  expect(Array.isArray(value), `${key} must be an array`).toBe(true);
  return value as Json[];
}

function record(payload: Json, key: string): Json {
  const value = payload[key];
  expect(value && typeof value === "object" && !Array.isArray(value), `${key} must be an object`).toBe(true);
  return value as Json;
}

function panel(page: Page, title: string) {
  return page.getByRole("heading", { level: 2, name: title, exact: true }).locator("..").locator("..");
}

function stockRow(page: Page) {
  return panel(page, "Stock on hand").locator("tbody tr").filter({ hasText: partNumber });
}

async function expectStock(page: Page, onHand: string, reserved: string, available: string) {
  const row = stockRow(page);
  await expect(row).toHaveCount(1);
  await expect(row.locator("td").nth(2)).toHaveText(onHand);
  await expect(row.locator("td").nth(3)).toHaveText(reserved);
  await expect(row.locator("td").nth(4)).toHaveText(available);
}

test("@inventory stock is adjusted, reserved, issued, returned, costed, and preserved in immutable histories", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  const diagnostics = collectBrowserDiagnostics(page);
  try {
    await loginAs(page, "purchasing.manager@example.com");
    const workOrders = rows(await getJson(page, "/api/v1/maintenance/work-orders/"), "work_orders");
    const workOrder = workOrders.find((item) => item.number === "WO-DEMO-1001");
    expect(workOrder, "deterministic open work order").toBeTruthy();
    const workOrderId = String(workOrder!.id);
    const baselineWorkOrder = record(
      await getJson(page, `/api/v1/maintenance/work-orders/${workOrderId}/`),
      "work_order",
    );
    const baselinePartCost = Number(baselineWorkOrder.part_cost);
    expect(Number.isFinite(baselinePartCost)).toBe(true);

    await page.goto("/parts");
    await expect(page.getByRole("heading", { level: 1, name: "Parts", exact: true })).toBeVisible();
    await page.locator("summary").filter({ hasText: "New part" }).click();
    await page.getByRole("textbox", { name: /^Part number/ }).fill(partNumber);
    await page.getByRole("textbox", { name: /^Part name/ }).fill(partName);
    await page.getByRole("textbox", { name: "Description", exact: true }).fill("Deterministic E2E inventory workflow part");
    await page.getByRole("textbox", { name: "Alternate part number", exact: true }).fill(alternateNumber);
    await page.getByRole("spinbutton", { name: "Standard cost", exact: true }).fill("12.50");
    const created = record(await submit(page, "/api/v1/inventory/parts/", "Create part"), "part");
    const partId = String(created.id);
    expect(created).toMatchObject({ number: partNumber, name: partName });
    expect(Number(created.default_unit_cost)).toBe(12.5);
    const partRow = panel(page, "Part master").locator("tbody tr").filter({ hasText: partNumber });
    await expect(partRow).toContainText(alternateNumber);
    await logout(page);

    await loginAs(page, "purchasing.manager@example.com");
    const bins = rows(await getJson(page, "/api/v1/inventory/bins/"), "bins");
    const bin = bins.find((item) => item.warehouse_code === "MAIN" && item.code === "A-01");
    expect(bin, "deterministic Main Parts Room / A-01 bin").toBeTruthy();
    const binId = String(bin!.id);
    await page.goto("/inventory");
    await expect(page.getByRole("heading", { level: 1, name: "Inventory", exact: true })).toBeVisible();
    await page.getByRole("tab", { name: "Adjust stock", exact: true }).click();
    await page.getByRole("combobox", { name: /^Part/ }).selectOption(partId);
    await page.getByRole("combobox", { name: /^Bin/ }).selectOption(binId);
    await page.getByRole("spinbutton", { name: /^Quantity change/ }).fill("10");
    await page.getByRole("textbox", { name: /^Reason/ }).fill("Auditable E2E opening balance");
    const adjustment = record(
      await submit(page, "/api/v1/inventory/adjustments/", "Adjust stock"),
      "transaction",
    );
    expect(adjustment).toMatchObject({ type: "ADJUSTMENT", part_id: partId, bin_id: binId, quantity: "10.000" });
    await expect(page.getByText("Adjust transaction recorded.", { exact: true })).toBeVisible();
    await expectStock(page, "10.000", "0.000", "10.000");
    await expect(stockRow(page).locator("input, select, textarea, button")).toHaveCount(0);
    await logout(page);

    await loginAs(page, "supervisor@example.com");
    await page.goto("/inventory");
    await expect(page.getByRole("heading", { level: 1, name: "Inventory", exact: true })).toBeVisible();
    await page.getByRole("tab", { name: "Reserve part", exact: true }).click();
    await page.getByRole("combobox", { name: /^Part/ }).selectOption(partId);
    await page.getByRole("combobox", { name: /^Bin/ }).selectOption(binId);
    await page.getByRole("textbox", { name: /^Work order ID/ }).fill(workOrderId);
    await page.getByRole("spinbutton", { name: /^Quantity/ }).fill("4");
    const reservation = record(
      await submit(page, "/api/v1/inventory/reservations/", "Reserve part"),
      "reservation",
    );
    const reservationId = String(reservation.id);
    expect(reservation).toMatchObject({
      part_id: partId,
      bin_id: binId,
      work_order_id: workOrderId,
      requested_quantity: "4.000",
      remaining_quantity: "4.000",
      status: "Active",
    });
    await expectStock(page, "10.000", "4.000", "6.000");
    await logout(page);

    await loginAs(page, "technician@example.com");
    await page.goto(`/parts?part_id=${partId}`);
    const technicianAvailability = panel(page, `${partNumber} availability`);
    await expect(technicianAvailability).toContainText("MAIN/A-01");
    await expect(technicianAvailability).toContainText("6.000");
    await expect(page.getByRole("columnheader", { name: "Standard cost", exact: true })).toHaveCount(0);
    await page.goto("/inventory");
    await expect(page.getByRole("heading", { level: 1, name: "Inventory", exact: true })).toBeVisible();
    await expect(stockRow(page)).toContainText("MAIN/A-01");
    await page.getByRole("tab", { name: "Issue part", exact: true }).click();
    await page.getByRole("combobox", { name: /^Part/ }).selectOption(partId);
    await page.getByRole("combobox", { name: /^Bin/ }).selectOption(binId);
    await page.getByRole("textbox", { name: /^Work order ID/ }).fill(workOrderId);
    await page.getByRole("combobox", { name: "Reservation", exact: true }).selectOption(reservationId);
    await page.getByRole("spinbutton", { name: /^Quantity/ }).fill("3");
    const issue = record(await submit(page, "/api/v1/inventory/issues/", "Issue part"), "transaction");
    const issueId = String(issue.id);
    expect(issue).toMatchObject({
      type: "ISSUE",
      part_id: partId,
      bin_id: binId,
      work_order_id: workOrderId,
      reservation_id: reservationId,
      quantity: "-3.000",
    });
    expect(issue).not.toHaveProperty("unit_cost");
    expect(issue).not.toHaveProperty("total_cost");
    await expectStock(page, "7.000", "1.000", "6.000");

    await page.getByRole("tab", { name: "Return part", exact: true }).click();
    await page.getByRole("combobox", { name: /^Part/ }).selectOption(partId);
    await page.getByRole("combobox", { name: /^Bin/ }).selectOption(binId);
    await page.getByRole("textbox", { name: /^Original issue transaction ID/ }).fill(issueId);
    await page.getByRole("spinbutton", { name: /^Quantity/ }).fill("1");
    await page.getByRole("textbox", { name: /^Reason/ }).fill("Unused E2E unit returned to stock");
    const returned = record(await submit(page, "/api/v1/inventory/returns/", "Return part"), "transaction");
    const returnId = String(returned.id);
    expect(returned).toMatchObject({
      type: "RETURN",
      part_id: partId,
      bin_id: binId,
      work_order_id: workOrderId,
      original_transaction_id: issueId,
      quantity: "1.000",
    });
    expect(returned).not.toHaveProperty("unit_cost");
    expect(returned).not.toHaveProperty("total_cost");
    await expect(page.getByText("Return transaction recorded.", { exact: true })).toBeVisible();
    await expectStock(page, "8.000", "1.000", "7.000");

    const csrfResponse = await page.context().request.get("/api/v1/auth/csrf/");
    expect(csrfResponse.status(), await csrfResponse.text()).toBe(200);
    const csrf = String((await csrfResponse.json()).csrf_token);
    const directEdit = await page.context().request.patch("/api/v1/inventory/stock/", {
      headers: { "X-CSRFToken": csrf, "Idempotency-Key": "7638a611-f245-520d-a8d8-54a66c338358" },
      data: { part_id: partId, bin_id: binId, quantity_on_hand: "999" },
    });
    expect(directEdit.status(), await directEdit.text()).toBe(405);

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { level: 1, name: "Inventory", exact: true })).toBeVisible();
    await expectStock(page, "8.000", "1.000", "7.000");
    await page.getByRole("combobox", { name: "History bin", exact: true }).selectOption(binId);
    const visibleBinHistory = panel(page, "Bin transaction history").locator("tbody tr").filter({ hasText: partNumber });
    await expect(visibleBinHistory).toHaveCount(2);
    await expect(visibleBinHistory.filter({ hasText: "Issue" })).toContainText("-3.000");
    await expect(visibleBinHistory.filter({ hasText: "Return" })).toContainText("1.000");
    await logout(page);

    await loginAs(page, "parts.clerk@example.com");
    await page.goto("/inventory");
    await page.getByRole("combobox", { name: "Yard", exact: true }).selectOption({ index: 1 });
    await page.getByRole("textbox", { name: /^Storage area code\b/ }).fill(storageAreaCode);
    await page.getByRole("textbox", { name: "Storage area name", exact: true }).fill("E2E parts cage");
    const storageArea = record(
      await submit(page, "/api/v1/inventory/warehouses/", "Create storage area"),
      "warehouse",
    );
    await page.getByRole("combobox", { name: "Storage area", exact: true }).selectOption(String(storageArea.id));
    await page.getByRole("textbox", { name: /^Physical address\b/ }).fill(physicalAddress);
    await page.getByRole("textbox", { name: "Location description", exact: true }).fill("Cabinet 2, shelf 3, drawer 1");
    const physicalLocation = record(
      await submit(page, "/api/v1/inventory/bins/", "Create physical location"),
      "bin",
    );
    const historyBin = page.getByRole("combobox", { name: "History bin", exact: true });
    await expect(historyBin.locator("option", { hasText: `${storageAreaCode}/${physicalAddress}` })).toHaveCount(1);
    await historyBin.selectOption(String(physicalLocation.id));
    await expect(panel(page, "Bin transaction history")).toContainText("No transactions found for this bin.");
    await page.getByRole("combobox", { name: "History bin", exact: true }).selectOption(binId);
    const completeBinHistory = panel(page, "Bin transaction history").locator("tbody tr").filter({ hasText: partNumber });
    await expect(completeBinHistory).toHaveCount(3);
    await expect(completeBinHistory.filter({ hasText: "Adjustment" })).toContainText("10.000");
    await page.goto("/parts");
    const historyResponse = page.waitForResponse(
      (response) => response.url().endsWith(`/api/v1/inventory/parts/${partId}/history/`),
    );
    await panel(page, "Part master").getByRole("button", { name: partNumber, exact: true }).click();
    expect((await historyResponse).status()).toBe(200);
    const visibleHistory = panel(page, `${partNumber} history`);
    const clerkAvailability = panel(page, `${partNumber} availability`);
    await expect(visibleHistory.getByText("RETURN: 1.000", { exact: true })).toBeVisible();
    await expect(visibleHistory.getByText("ISSUE: -3.000", { exact: true })).toBeVisible();
    await expect(visibleHistory.getByText("ADJUSTMENT: 10.000", { exact: true })).toBeVisible();
    await expect(clerkAvailability).toContainText("MAIN/A-01");
    await expect(clerkAvailability).toContainText("7.000");
    await expect(visibleHistory).toContainText("MAIN/A-01");
    await expect(page.getByRole("columnheader", { name: "Standard cost", exact: true })).toHaveCount(0);

    const partHistory = await getJson(page, `/api/v1/inventory/parts/${partId}/history/`);
    const transactions = rows(partHistory, "transactions");
    expect(transactions.map((item) => item.id)).toEqual([returnId, issueId, adjustment.id]);
    expect(transactions.find((item) => item.id === returnId)).toMatchObject({ original_transaction_id: issueId });
    expect(rows(partHistory, "balances")).toEqual([
      expect.objectContaining({
        part_id: partId,
        bin_id: binId,
        quantity_on_hand: "8.000",
        quantity_reserved: "1.000",
        available_quantity: "7.000",
      }),
    ]);
    expect(rows(partHistory, "reservations")).toEqual([
      expect.objectContaining({
        id: reservationId,
        requested_quantity: "4.000",
        issued_quantity: "3.000",
        remaining_quantity: "1.000",
        status: "PartiallyIssued",
      }),
    ]);

    const binHistory = rows(await getJson(page, `/api/v1/inventory/bins/${binId}/history/`), "transactions");
    expect(binHistory.filter((item) => item.part_id === partId).map((item) => item.id)).toEqual([
      returnId,
      issueId,
      adjustment.id,
    ]);

    await logout(page);
    await loginAs(page, "fleet.manager@example.com");
    const workOrderDetail = record(
      await getJson(page, `/api/v1/maintenance/work-orders/${workOrderId}/`),
      "work_order",
    );
    expect(Number(workOrderDetail.part_cost)).toBeCloseTo(baselinePartCost + 25, 4);
    const workOrderTransactions = workOrderDetail.stock_transactions as Json[];
    expect(workOrderTransactions.map((item) => item.id)).toEqual(expect.arrayContaining([issueId, returnId]));
    expect(workOrderTransactions.find((item) => item.id === issueId)).toMatchObject({ total_cost: "37.5000" });
    expect(workOrderTransactions.find((item) => item.id === returnId)).toMatchObject({
      original_transaction_id: issueId,
      total_cost: "-12.5000",
    });
    await logout(page);

    await loginAs(page, "fleet.manager@example.com");
    await page.goto(`/work-orders/${workOrderId}/`);
    await expect(page.getByRole("heading", { level: 1, name: /WO-DEMO-1001/ })).toBeVisible();
    const visibleWorkOrderHistory = panel(page, "Work-order parts history");
    await expect(visibleWorkOrderHistory.locator(".cost-summary")).toContainText(`$${String(workOrderDetail.part_cost)}`);
    const visibleWorkOrderTransactions = visibleWorkOrderHistory.locator("tbody tr").filter({ hasText: partNumber });
    await expect(visibleWorkOrderTransactions).toHaveCount(2);
    await expect(visibleWorkOrderTransactions.filter({ hasText: "Issue" })).toContainText("-3.000");
    await expect(visibleWorkOrderTransactions.filter({ hasText: "Issue" })).toContainText("$37.5000");
    await expect(visibleWorkOrderTransactions.filter({ hasText: "Return" })).toContainText("1.000");
    await expect(visibleWorkOrderTransactions.filter({ hasText: "Return" })).toContainText("$-12.5000");

    const events = rows(await getJson(page, "/api/v1/audit-events/"), "events");
    expect(events.find((item) => item.resource_id === String(adjustment.id))).toMatchObject({
      action: "stock.adjusted",
      actor: "purchasing.manager@example.com",
    });
    expect(events.find((item) => item.resource_id === reservationId)).toMatchObject({
      action: "stock.reserved",
      actor: "supervisor@example.com",
    });
    expect(events.find((item) => item.resource_id === issueId)).toMatchObject({
      action: "stock.issued",
      actor: "technician@example.com",
    });
    expect(events.find((item) => item.resource_id === returnId)).toMatchObject({
      action: "stock.returned",
      actor: "technician@example.com",
    });
    await logout(page);
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});
