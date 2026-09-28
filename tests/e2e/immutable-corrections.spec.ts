import { createHash } from "node:crypto";

import { expect, type Dialog, type Locator, type Page, type TestInfo, test } from "@playwright/test";

import { collectBrowserDiagnostics } from "./helpers";

type Json = Record<string, unknown>;

const password = process.env.E2E_PASSWORD ?? "DemoPass123!";
const manager = process.env.E2E_USERNAME ?? "fleet.manager@example.com";

function object(value: unknown, label = "value"): Json {
  expect(value, `${label} must be an object`).toBeTruthy();
  expect(typeof value, `${label} must be an object`).toBe("object");
  expect(Array.isArray(value), `${label} must be an object`).toBe(false);
  return value as Json;
}

function rows(payload: Json, key: string): Json[] {
  const value = payload[key];
  expect(Array.isArray(value), `${key} must be an array`).toBe(true);
  return (value as unknown[]).map((row) => object(row, `${key} row`));
}

function nested(payload: Json, key: string): Json {
  return object(payload[key], key);
}

function id(row: Json): string {
  const value = String(row.id ?? "");
  expect(value).toMatch(/^[0-9a-f-]{36}$/i);
  return value;
}

function suffix(testInfo: TestInfo): string {
  return createHash("sha256")
    .update(`${process.env.E2E_RUN_ID ?? "manual"}:${testInfo.testId}`)
    .digest("hex")
    .slice(0, 10)
    .toUpperCase();
}

function operationId(scope: string, name: string): string {
  const hex = createHash("sha256").update(`${scope}:${name}`).digest("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-8${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

async function loginAs(page: Page, username: string): Promise<void> {
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
}

async function logout(page: Page): Promise<void> {
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/logout/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBe(200);
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
}

async function getJson(page: Page, path: string): Promise<Json> {
  const response = await page.context().request.get(path);
  const body = await response.text();
  expect(response.status(), body).toBe(200);
  return object(JSON.parse(body), path);
}

async function mutateJson(
  page: Page,
  method: "POST" | "PATCH",
  path: string,
  data: Json,
  key: string,
): Promise<Json> {
  const csrfResponse = await page.context().request.get("/api/v1/auth/csrf/");
  expect(csrfResponse.status(), await csrfResponse.text()).toBe(200);
  const csrf = String(object(await csrfResponse.json()).csrf_token ?? "");
  expect(csrf).not.toBe("");
  const response = await page.context().request.fetch(path, {
    method,
    data,
    headers: { Accept: "application/json", "Idempotency-Key": key, "X-CSRFToken": csrf },
  });
  const body = await response.text();
  expect(response.status(), body).toBeGreaterThanOrEqual(200);
  expect(response.status(), body).toBeLessThan(300);
  return object(JSON.parse(body), path);
}

async function uiMutation(
  page: Page,
  method: string,
  path: string,
  action: () => Promise<unknown>,
): Promise<Json> {
  const responsePromise = page.waitForResponse(
    (response) => new URL(response.url()).pathname === path && response.request().method() === method,
  );
  await action();
  const response = await responsePromise;
  const body = await response.text();
  expect(response.status(), body).toBeGreaterThanOrEqual(200);
  expect(response.status(), body).toBeLessThan(300);
  return object(JSON.parse(body), path);
}

async function withPrompts<T>(
  page: Page,
  answers: Array<{ message: RegExp; answer: string }>,
  action: () => Promise<T>,
): Promise<T> {
  let index = 0;
  const handler = async (dialog: Dialog) => {
    const expected = answers[index++];
    expect(expected, `Unexpected dialog: ${dialog.message()}`).toBeTruthy();
    expect(dialog.message()).toMatch(expected.message);
    await dialog.accept(expected.answer);
  };
  page.on("dialog", handler);
  try {
    const result = await action();
    expect(index, "all expected prompts were shown").toBe(answers.length);
    return result;
  } finally {
    page.off("dialog", handler);
  }
}

async function selectContaining(select: Locator, text: string): Promise<void> {
  const option = select.locator("option").filter({ hasText: text }).first();
  await expect(option).toBeAttached();
  const value = await option.getAttribute("value");
  expect(value, `option containing ${text}`).toBeTruthy();
  await select.selectOption(value!);
}

function panel(page: Page, title: string): Locator {
  return page.getByRole("heading", { level: 2, name: title, exact: true }).locator("..").locator("..");
}

async function expectAudit(
  page: Page,
  resourceType: string,
  resourceId: string,
  action: string,
  transition?: string,
): Promise<Json> {
  const payload = await getJson(
    page,
    `/api/v1/audit-events/?resource_type=${encodeURIComponent(resourceType)}&resource_id=${encodeURIComponent(resourceId)}`,
  );
  const event = rows(payload, "events").find((candidate) => candidate.action === action);
  expect(event, `${action} audit event`).toBeTruthy();

  await page.goto("/audit");
  await page.getByLabel(/^Resource type/).fill(resourceType);
  let auditRow = panel(page, "Append-only events")
    .locator("tbody tr")
    .filter({ hasText: action })
    .filter({ hasText: resourceId });
  if (transition) auditRow = auditRow.filter({ hasText: transition });
  await expect(auditRow).toHaveCount(1);
  return event!;
}

test("meter correction appends a superseding reading and preserves the original", async ({ page }, testInfo) => {
  const diagnostics = collectBrowserDiagnostics(page);
  const scope = suffix(testInfo);
  const unitNumber = `COR-MTR-${scope}`;
  try {
    await loginAs(page, manager);
    await page.goto("/assets");
    await page.getByText("New asset", { exact: true }).click();
    await page.getByLabel(/^Unit number/).fill(unitNumber);
    await page.getByLabel(/^Truck type/).fill("Truck");
    const created = nested(
      await uiMutation(page, "POST", "/api/v1/assets/", () =>
        page.getByRole("button", { name: "Create asset", exact: true }).click(),
      ),
      "asset",
    );
    const assetId = id(created);

    await page.getByRole("link", { name: unitNumber, exact: true }).click();
    await page.getByRole("combobox", { name: "New meter type", exact: true }).selectOption("odometer");
    await page.getByRole("combobox", { name: "Unit", exact: true }).selectOption("mi");
    await page.getByLabel(/^Reading/).fill("500");
    const meterPayload = await uiMutation(page, "POST", `/api/v1/assets/${assetId}/meters/`, () =>
      page.getByRole("button", { name: "Record meter", exact: true }).click(),
    );
    const original = nested(meterPayload, "reading");
    const originalId = id(original);

    const originalRow = panel(page, "Meter history").locator("tbody tr").filter({ hasText: "500.000 mi" });
    await expect(originalRow).toContainText("manual");
    const correctionReason = `Transcription correction ${scope}`;
    const correctedPayload = await withPrompts(
      page,
      [
        { message: /Corrected meter value/, answer: "575" },
        { message: /Reason for this correction/, answer: correctionReason },
      ],
      () =>
        uiMutation(page, "POST", `/api/v1/assets/meter-readings/${originalId}/correct/`, () =>
          originalRow.getByRole("button", { name: "Correct meter", exact: true }).click(),
        ),
    );
    const corrected = nested(correctedPayload, "reading");
    const correctedId = id(corrected);

    await expect(panel(page, "Meter history").locator("tbody tr").filter({ hasText: "500.000 mi" })).toHaveCount(1);
    await expect(panel(page, "Meter history").locator("tbody tr").filter({ hasText: "575.000 mi" })).toContainText("correction");
    await expect(page.getByRole("combobox", { name: "Existing meter", exact: true }).locator("option").filter({ hasText: "575.000 mi" })).toHaveCount(1);
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(panel(page, "Meter history").locator("tbody tr").filter({ hasText: "500.000 mi" })).toHaveCount(1);
    await expect(panel(page, "Meter history").locator("tbody tr").filter({ hasText: "575.000 mi" })).toHaveCount(1);

    const meterState = await getJson(page, `/api/v1/assets/${assetId}/meters/`);
    const meter = rows(meterState, "meters")[0];
    expect(meter.current_value).toBe("575.000");
    const readings = (meter.readings as unknown[]).map((row) => object(row));
    expect(readings).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: originalId, value: "500.000", corrects_id: null }),
      expect.objectContaining({ id: correctedId, value: "575.000", corrects_id: originalId, reason: correctionReason }),
    ]));

    const audit = await expectAudit(page, "MeterReading", correctedId, "meter.reading_corrected", "500.000 → 575");
    expect(object(audit.context).original_reading_id).toBe(originalId);
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});

test("stock correction is an append-only compensating adjustment with a corrected projection", async ({ page }, testInfo) => {
  const diagnostics = collectBrowserDiagnostics(page);
  const scope = suffix(testInfo);
  const partNumber = `COR-STK-${scope}`;
  const partName = `Correction test stock ${scope}`;
  try {
    await loginAs(page, "purchasing.manager@example.com");
    await page.goto("/parts");
    await page.getByText("New part", { exact: true }).click();
    await page.getByLabel(/^Part number/).fill(partNumber);
    await page.getByLabel(/^Part name/).fill(partName);
    await page.getByLabel(/^Standard cost/).fill("9.50");
    const part = nested(
      await uiMutation(page, "POST", "/api/v1/inventory/parts/", () =>
        page.getByRole("button", { name: "Create part", exact: true }).click(),
      ),
      "part",
    );
    const partId = id(part);
    await expect(panel(page, "Part master").getByRole("button", { name: partNumber, exact: true })).toBeVisible();
    await page.waitForLoadState("networkidle");

    await logout(page);
    await loginAs(page, "purchasing.manager@example.com");
    await page.goto("/inventory");
    await page.getByRole("tab", { name: "Adjust stock", exact: true }).click();

    const submitAdjustment = async (quantity: string, reason: string) => {
      await page.getByRole("tab", { name: "Adjust stock", exact: true }).click();
      await selectContaining(page.getByRole("combobox", { name: "Part", exact: true }), partNumber);
      await selectContaining(page.getByRole("combobox", { name: "Bin", exact: true }), "A-01");
      await page.getByLabel(/^Quantity change/).fill(quantity);
      await page.getByLabel(/^Reason/).fill(reason);
      return nested(
        await uiMutation(page, "POST", "/api/v1/inventory/adjustments/", () =>
          page.getByRole("button", { name: "Adjust stock", exact: true }).click(),
        ),
        "transaction",
      );
    };

    const opening = await submitAdjustment("10", `Auditable opening balance ${scope}`);
    const openingId = id(opening);
    const correctionReason = `Reverse overstated count ${scope}`;
    await page.getByRole("tab", { name: "Correct stock transaction", exact: true }).click();
    await page.getByLabel(/^Original stock transaction ID\b/).fill(openingId);
    await page.getByLabel(/^Reason/).fill(correctionReason);
    const correctionResponsePromise = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/v1/inventory/adjustments/"
        && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Correct stock transaction", exact: true }).click();
    const correctionResponse = await correctionResponsePromise;
    const correctionBody = await correctionResponse.text();
    expect(correctionResponse.status(), correctionBody).toBe(201);
    const correction = nested(object(JSON.parse(correctionBody)), "transaction");
    const correctionId = id(correction);
    expect(correction).toMatchObject({
      type: "REVERSAL",
      quantity: "-10.000",
      original_transaction_id: openingId,
      reason: correctionReason,
    });
    await expect(page.getByRole("status").filter({ hasText: "Correct transaction recorded." })).toBeVisible();

    const correctionHeaders = await correctionResponse.request().allHeaders();
    const correctionKey = correctionHeaders["idempotency-key"];
    expect(correctionKey).toMatch(/^[0-9a-f-]{36}$/i);
    const replayedCorrection = nested(await mutateJson(
      page,
      "POST",
      "/api/v1/inventory/adjustments/",
      { original_transaction_id: openingId, reason: correctionReason },
      correctionKey,
    ), "transaction");
    expect(id(replayedCorrection)).toBe(correctionId);

    const replacementReason = `Corrected count replacement ${scope}`;
    const replacement = await submitAdjustment("7.5", replacementReason);
    const replacementId = id(replacement);

    const balanceRow = panel(page, "Stock on hand").locator("tbody tr").filter({ hasText: partNumber });
    await expect(balanceRow.locator("td").nth(2)).toHaveText("7.500");
    await expect(balanceRow.locator("td").nth(4)).toHaveText("7.500");
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(panel(page, "Stock on hand").locator("tbody tr").filter({ hasText: partNumber }).locator("td").nth(2)).toHaveText("7.500");

    await page.goto("/parts");
    await page.getByRole("button", { name: partNumber, exact: true }).click();
    const history = panel(page, `${partNumber} history`);
    await expect(history.getByText("ADJUSTMENT: 10.000", { exact: true })).toBeVisible();
    await expect(history.getByText("REVERSAL: -10.000", { exact: true })).toBeVisible();
    await expect(history.getByText("ADJUSTMENT: 7.500", { exact: true })).toBeVisible();

    const durable = await getJson(page, `/api/v1/inventory/parts/${partId}/history/`);
    expect(rows(durable, "transactions")).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: openingId, quantity: "10.000" }),
      expect.objectContaining({ id: correctionId, type: "REVERSAL", quantity: "-10.000", reason: correctionReason, original_transaction_id: openingId }),
      expect.objectContaining({ id: replacementId, type: "ADJUSTMENT", quantity: "7.500", reason: replacementReason }),
    ]));
    expect(rows(durable, "transactions").filter((row) => row.id === correctionId)).toHaveLength(1);
    expect(rows(durable, "balances")[0]).toEqual(expect.objectContaining({ quantity_on_hand: "7.500", available_quantity: "7.500" }));

    await logout(page);
    await loginAs(page, manager);
    const audit = await expectAudit(page, "StockTransaction", correctionId, "stock.reversed");
    expect(Number(object(audit.context).quantity)).toBe(-10);
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});

test("posted receipt reversal preserves both receipts and compensating ledger records", async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  const diagnostics = collectBrowserDiagnostics(page);
  const scope = suffix(testInfo);
  const vendorCode = `COR-${scope}`;
  const vendorName = `Correction Supply ${scope}`;
  const purchaseOrderNumber = `PO-COR-${scope}`;
  try {
    await loginAs(page, "purchasing.manager@example.com");
    const bootstrap = await getJson(page, "/api/v1/bootstrap/");
    const part = rows(bootstrap, "parts").find((row) => row.number === "OIL-15W40");
    expect(part, "seeded oil part").toBeTruthy();
    const bins = rows(await getJson(page, "/api/v1/inventory/bins/"), "bins");
    const stockBin = bins.find((row) => row.code === "A-01");
    expect(stockBin, "seeded A-01 bin").toBeTruthy();

    const vendor = nested(
      await mutateJson(page, "POST", "/api/v1/purchasing/vendors/", {
        code: vendorCode,
        name: vendorName,
      }, operationId(scope, "vendor")),
      "vendor",
    );
    const order = nested(
      await mutateJson(page, "POST", "/api/v1/purchasing/purchase-orders/", {
        vendor_id: id(vendor),
        number: purchaseOrderNumber,
        lines: [{ part_id: id(part!), quantity_ordered: "100", unit_cost: "20.00" }],
      }, operationId(scope, "purchase-order")),
      "purchase_order",
    );
    const orderId = id(order);
    const submitted = nested(
      await mutateJson(page, "POST", `/api/v1/purchasing/purchase-orders/${orderId}/transition/`, {
        target: "Submitted",
      }, operationId(scope, "submit-order")),
      "purchase_order",
    );
    expect(submitted.status).toBe("Submitted");

    await logout(page);
    await loginAs(page, "purchasing.approver@example.com");
    const approved = nested(
      await mutateJson(page, "POST", `/api/v1/purchasing/purchase-orders/${orderId}/transition/`, {
        target: "Approved",
      }, operationId(scope, "approve-order")),
      "purchase_order",
    );
    expect(approved.status).toBe("Approved");

    await logout(page);
    await loginAs(page, "purchasing.manager@example.com");
    await mutateJson(page, "POST", `/api/v1/purchasing/purchase-orders/${orderId}/transition/`, {
      target: "Sent",
    }, operationId(scope, "send-order"));
    const orderLine = (order.lines as unknown[]).map((row) => object(row))[0];
    const posted = nested(
      await mutateJson(page, "POST", "/api/v1/purchasing/receipts/", {
        purchase_order_id: orderId,
        packing_slip: `PS-${scope}`,
        lines: [{ purchase_order_line_id: id(orderLine), bin_id: id(stockBin!), quantity: "3" }],
      }, operationId(scope, "post-receipt")),
      "receipt",
    );
    const postedId = id(posted);
    const postedNumber = String(posted.number);

    await page.goto("/purchase-orders");
    const receiptRow = panel(page, "Receipt history").locator(".workflow-row").filter({ hasText: `${postedNumber} · ${purchaseOrderNumber}` });
    await expect(receiptRow).toContainText("Posted");
    const reversalReason = `Wrong shipment ${scope}`;
    const reversal = nested(
      await withPrompts(
        page,
        [{ message: /Reason for reversing this receipt/, answer: reversalReason }],
        () =>
          uiMutation(page, "POST", `/api/v1/purchasing/receipts/${postedId}/reverse/`, () =>
            receiptRow.getByRole("button", { name: "Reverse receipt", exact: true }).click(),
          ),
      ),
      "receipt",
    );
    const reversalId = id(reversal);
    const reversalNumber = String(reversal.number);
    await expect(page.getByRole("status").filter({ hasText: "Receipt reversed with compensating stock transactions." })).toBeVisible();
    const originalReceiptRow = panel(page, "Receipt history").locator(".workflow-row").filter({
      has: page.getByText(`${postedNumber} · ${purchaseOrderNumber}`, { exact: true }),
    });
    const reversalReceiptRow = panel(page, "Receipt history").locator(".workflow-row").filter({
      has: page.getByText(`${reversalNumber} · ${purchaseOrderNumber}`, { exact: true }),
    });
    await expect(originalReceiptRow).toContainText("Reversed");
    await expect(reversalReceiptRow).toContainText("Posted");

    await page.reload({ waitUntil: "domcontentloaded" });
    const orderRow = panel(page, "Purchase orders").locator("article.workflow-row").filter({ hasText: `${purchaseOrderNumber} · ${vendorName}` });
    await expect(orderRow).toContainText("Sent");

    const receiptState = rows(await getJson(page, "/api/v1/purchasing/receipts/"), "receipts");
    const originalState = receiptState.find((row) => row.id === postedId);
    const reversalState = receiptState.find((row) => row.id === reversalId);
    expect(originalState).toEqual(expect.objectContaining({ status: "Reversed", reason: "" }));
    expect(reversalState).toEqual(expect.objectContaining({ status: "Posted", reversal_of_id: postedId, reason: reversalReason }));
    const originalLine = (object(originalState!).lines as unknown[]).map((row) => object(row))[0];
    const reversalLine = (object(reversalState!).lines as unknown[]).map((row) => object(row))[0];
    expect(reversalLine).toEqual(expect.objectContaining({ quantity: "-3.000", reversal_of_id: id(originalLine) }));
    const ledger = rows(await getJson(page, `/api/v1/inventory/parts/${id(part!)}/history/`), "transactions");
    expect(ledger).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: String(originalLine.stock_transaction_id), type: "RECEIPT", quantity: "3.000" }),
      expect.objectContaining({ id: String(reversalLine.stock_transaction_id), type: "REVERSAL", quantity: "-3.000", original_transaction_id: String(originalLine.stock_transaction_id) }),
    ]));

    await logout(page);
    await loginAs(page, manager);
    const audit = await expectAudit(page, "Receipt", postedId, "receipt.reversed", "Posted → Reversed");
    expect(object(audit.context)).toEqual(expect.objectContaining({ reversal_id: reversalId, reason: reversalReason }));
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});

test("submitted inspection is voided and replaced without hiding its original", async ({ page }, testInfo) => {
  const diagnostics = collectBrowserDiagnostics(page);
  const scope = suffix(testInfo);
  const templateName = `Correction Inspection ${scope}`;
  const questionId = `correction-${scope.toLowerCase()}`;
  try {
    await loginAs(page, "supervisor@example.com");
    const bootstrap = await getJson(page, "/api/v1/bootstrap/");
    const asset = rows(bootstrap, "assets").find((row) => row.unit_number === "TRK-007");
    expect(asset, "seeded TRK-007 asset").toBeTruthy();
    const assetId = id(asset!);

    const template = nested(
      await mutateJson(page, "POST", "/api/v1/maintenance/inspection-templates/", {
        name: templateName,
        questions: [{ id: questionId, label: `Correction check ${scope}`, type: "pass_fail", required: true }],
      }, operationId(scope, "inspection-template")),
      "inspection_template",
    );
    const templateId = id(template);
    const original = nested(
      await mutateJson(page, "POST", "/api/v1/maintenance/inspections/", {
        asset_id: assetId,
        template_id: templateId,
        responses: [{ question_id: questionId, result: "pass", notes: `Original response ${scope}` }],
        acknowledgment: "I confirm this inspection is complete and accurate.",
        submit: true,
      }, operationId(scope, "original-inspection")),
      "inspection",
    );
    const originalId = id(original);

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.goto("/inspections");
    const originalRow = panel(page, "Inspection history").locator(".workflow-row").filter({ hasText: `TRK-007 · ${templateName}` });
    await expect(originalRow).toContainText("Submitted");
    const voidReason = `Duplicate submission ${scope}`;
    await withPrompts(
      page,
      [{ message: /Reason for voiding and replacing this inspection/, answer: voidReason }],
      () =>
        uiMutation(page, "POST", `/api/v1/maintenance/inspections/${originalId}/void/`, () =>
          originalRow.getByRole("button", { name: "Void and replace", exact: true }).click(),
        ),
    );
    await expect(page.getByRole("status").filter({ hasText: "Original inspection was voided and remains in history." })).toBeVisible();

    await page.getByRole("combobox", { name: "Asset", exact: true }).selectOption(assetId);
    await page.getByRole("combobox", { name: "Inspection template", exact: true }).selectOption(templateId);
    await page.getByRole("radio", { name: "Pass", exact: true }).check();
    await page.getByLabel(/^Item note/).fill(`Replacement response ${scope}`);
    await page.getByRole("checkbox", { name: /I confirm this inspection is complete and accurate/ }).check();
    const replacement = nested(
      await uiMutation(page, "POST", "/api/v1/maintenance/inspections/", () =>
        page.getByRole("button", { name: "Submit replacement inspection", exact: true }).click(),
      ),
      "inspection",
    );
    const replacementId = id(replacement);

    const historyRows = panel(page, "Inspection history").locator(".workflow-row").filter({ hasText: `TRK-007 · ${templateName}` });
    await expect(historyRows).toHaveCount(2);
    await expect(historyRows.filter({ hasText: "Voided" })).toHaveCount(1);
    await expect(historyRows.filter({ hasText: "Submitted" })).toHaveCount(1);
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(panel(page, "Inspection history").locator(".workflow-row").filter({ hasText: `TRK-007 · ${templateName}` })).toHaveCount(2);

    const state = rows(await getJson(page, `/api/v1/maintenance/inspections/?asset_id=${assetId}`), "inspections");
    expect(state.find((row) => row.id === originalId)).toEqual(expect.objectContaining({ status: "Voided", void_reason: voidReason, replaces_id: null }));
    expect(state.find((row) => row.id === replacementId)).toEqual(expect.objectContaining({ status: "Submitted", replaces_id: originalId, asset_id: assetId }));
    await expectAudit(page, "Inspection", originalId, "inspection.voided", "Submitted → Voided");
    const replacementAudit = rows(
      await getJson(page, `/api/v1/audit-events/?resource_type=Inspection&resource_id=${replacementId}`),
      "events",
    );
    expect(replacementAudit).toEqual(expect.arrayContaining([expect.objectContaining({ action: "inspection.submitted", new_state: "Submitted" })]));
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});

test("closed work order reopens only with a reason while closure remains auditable", async ({ page }, testInfo) => {
  const diagnostics = collectBrowserDiagnostics(page);
  const scope = suffix(testInfo);
  const summary = `Correction reopen ${scope}`;
  try {
    await loginAs(page, "supervisor@example.com");
    const bootstrap = await getJson(page, "/api/v1/bootstrap/");
    const asset = rows(bootstrap, "assets").find((row) => row.unit_number === "TRL-003");
    expect(asset, "seeded trailer").toBeTruthy();
    const workOrder = nested(
      await mutateJson(page, "POST", "/api/v1/maintenance/work-orders/", {
        asset_id: id(asset!), summary, priority: "normal", requires_qc: false,
      }, operationId(scope, "work-order")),
      "work_order",
    );
    const workOrderId = id(workOrder);
    const workOrderNumber = String(workOrder.number);
    for (const [index, status] of ["Ready", "InProgress", "Completed", "Closed"].entries()) {
      await mutateJson(
        page,
        "POST",
        `/api/v1/maintenance/work-orders/${workOrderId}/transition/`,
        status === "Completed" ? { status, completion_summary: `Completed before correction ${scope}` } : { status },
        operationId(scope, `transition-${index}-${status}`),
      );
    }

    await page.goto(`/work-orders/${workOrderId}`);
    await expect(page.getByRole("heading", { level: 1, name: `${workOrderNumber} · TRL-003`, exact: true })).toBeVisible();
    await expect(page.getByText("Closed", { exact: true })).toBeVisible();
    const reopenReason = `Follow-up repair required ${scope}`;
    await withPrompts(
      page,
      [{ message: /Reason for reopening this closed work order/, answer: reopenReason }],
      () =>
        uiMutation(page, "POST", `/api/v1/maintenance/work-orders/${workOrderId}/transition/`, () =>
          page.getByRole("button", { name: "Reopen work order", exact: true }).click(),
        ),
    );
    await expect(page.getByText("Reopened", { exact: true })).toBeVisible();
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.getByText("Reopened", { exact: true })).toBeVisible();

    const durable = nested(await getJson(page, `/api/v1/maintenance/work-orders/${workOrderId}/`), "work_order");
    expect(durable).toEqual(expect.objectContaining({ status: "Reopened", reopen_reason: reopenReason }));
    expect(durable.closed_at, "original closure timestamp remains present").toBeTruthy();
    const audit = await expectAudit(page, "WorkOrder", workOrderId, "work_order.transitioned", "Closed → Reopened");
    expect(object(audit.context).reason).toBe(reopenReason);
    const events = rows(
      await getJson(page, `/api/v1/audit-events/?resource_type=WorkOrder&resource_id=${workOrderId}`),
      "events",
    );
    expect(events).toEqual(expect.arrayContaining([
      expect.objectContaining({ action: "work_order.transitioned", previous_state: "Completed", new_state: "Closed" }),
      expect.objectContaining({ action: "work_order.transitioned", previous_state: "Closed", new_state: "Reopened" }),
    ]));
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});
