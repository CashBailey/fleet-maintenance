import { expect, type Locator, type Page, test } from "@playwright/test";

import { collectBrowserDiagnostics } from "./helpers";

const password = process.env.E2E_PASSWORD ?? "DemoPass123!";
const buyer = "purchasing.manager@example.com";
const approver = "purchasing.approver@example.com";
const receiver = "parts.clerk@example.com";
const auditor = process.env.E2E_USERNAME ?? "fleet.manager@example.com";

type Json = Record<string, unknown>;
type JsonResponse = {
  status(): number;
  text(): Promise<string>;
  json(): Promise<unknown>;
};

function record(value: unknown): Json {
  expect(value).toBeTruthy();
  expect(typeof value).toBe("object");
  expect(Array.isArray(value)).toBe(false);
  return value as Json;
}

function list(value: unknown, key: string): Json[] {
  const rows = record(value)[key];
  expect(Array.isArray(rows)).toBe(true);
  return (rows as unknown[]).map(record);
}

function id(value: Json): string {
  expect(value.id).toEqual(expect.any(String));
  return String(value.id);
}

function quantity(value: unknown): number {
  const parsed = Number(value);
  expect(Number.isFinite(parsed), `Expected a numeric quantity, received ${String(value)}`).toBe(true);
  return parsed;
}

async function json(response: JsonResponse): Promise<Json> {
  expect(response.status(), await response.text()).toBeLessThan(300);
  return record(await response.json());
}

async function loginAs(page: Page, username: string): Promise<Json> {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password);
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/login/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign in" }).click();
  const payload = await json(await responsePromise);
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  return record(payload.user);
}

async function signOut(page: Page): Promise<void> {
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/logout/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign out" }).click();
  expect((await responsePromise).status()).toBe(200);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
}

function purchaseOrderRow(page: Page, number: string, vendor: string): Locator {
  return page.locator("article.workflow-row").filter({
    has: page.getByText(`${number} · ${vendor}`, { exact: true }),
  });
}

function receiptRow(page: Page, number: string, purchaseOrder: string): Locator {
  return page.locator(".workflow-row").filter({
    has: page.getByText(`${number} · ${purchaseOrder}`, { exact: true }),
  });
}

async function stockFor(page: Page, partId: string, binId: string): Promise<Json> {
  const response = await page.context().request.get(
    `/api/v1/inventory/stock/?part_id=${partId}&bin_id=${binId}`,
  );
  const rows = list(await json(response), "stock");
  expect(rows).toHaveLength(1);
  return rows[0];
}

test("purchasing approval, partial receipts, retry safety, and reversal stay auditable", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  const diagnostics = collectBrowserDiagnostics(page);
  const identifiers: Json = {
    vendor_code: "E2E-PURCH-01",
    part_number: "BRK-PAD-22",
    bin_code: "B-02",
  };

  try {
    await test.step("create the vendor and approval-required order through the UI", async () => {
      const buyingUser = await loginAs(page, buyer);
      expect(buyingUser.roles).toEqual(expect.arrayContaining(["purchasing_manager"]));

      const bootstrap = await json(await page.context().request.get("/api/v1/bootstrap/"));
      const part = list(bootstrap, "parts").find((row) => row.number === identifiers.part_number);
      expect(part, "deterministic brake part").toBeTruthy();
      identifiers.part_id = id(part!);

      const bins = list(await json(await page.context().request.get("/api/v1/inventory/bins/")), "bins");
      const stockBin = bins.find((row) => row.code === identifiers.bin_code);
      expect(stockBin, "deterministic receiving bin").toBeTruthy();
      identifiers.bin_id = id(stockBin!);

      const opening = await stockFor(page, String(identifiers.part_id), String(identifiers.bin_id));
      identifiers.opening_on_hand = opening.quantity_on_hand;
      identifiers.opening_reserved = opening.quantity_reserved;

      await page.goto("/vendors");
      await expect(page.getByRole("heading", { name: "Vendors", exact: true, level: 1 })).toBeVisible();
      await page.getByLabel("Vendor code").fill(String(identifiers.vendor_code));
      await page.getByLabel("Vendor name").fill("E2E Brake Supply");
      await page.getByLabel("Email").fill("e2e-brakes@example.invalid");
      const vendorResponsePromise = page.waitForResponse(
        (response) => response.url().endsWith("/api/v1/purchasing/vendors/") && response.request().method() === "POST",
      );
      await page.getByRole("button", { name: "Create vendor" }).click();
      const vendorPayload = await json(await vendorResponsePromise);
      const vendor = record(vendorPayload.vendor);
      identifiers.vendor_id = id(vendor);
      expect(vendor).toMatchObject({ code: identifiers.vendor_code, name: "E2E Brake Supply" });
      await expect(page.getByText("E2E-PURCH-01 · E2E Brake Supply", { exact: true })).toBeVisible();

      await page.getByRole("tab", { name: "Purchase orders" }).click();
      await page.getByRole("combobox", { name: "Vendor", exact: true }).selectOption(String(identifiers.vendor_id));
      await page.getByRole("combobox", { name: "Part", exact: true }).selectOption(String(identifiers.part_id));
      await page.getByLabel("Quantity ordered").fill("6");
      await page.getByLabel("Unit cost").fill("200");
      await page.getByLabel("Notes").fill("E2E required approval and staged receiving");
      const orderResponsePromise = page.waitForResponse(
        (response) => response.url().endsWith("/api/v1/purchasing/purchase-orders/") && response.request().method() === "POST",
      );
      await page.getByRole("button", { name: "Create purchase order" }).click();
      const order = record((await json(await orderResponsePromise)).purchase_order);
      identifiers.purchase_order_id = id(order);
      identifiers.purchase_order_number = order.number;
      const lines = order.lines as unknown[];
      expect(lines).toHaveLength(1);
      identifiers.purchase_order_line_id = id(record(lines[0]));
      expect(order.status).toBe("Draft");
      expect(quantity(order.total)).toBe(1200);

      const row = purchaseOrderRow(page, String(order.number), "E2E Brake Supply");
      await expect(row).toBeVisible();
      const submitResponsePromise = page.waitForResponse(
        (response) => response.url().includes(`/purchase-orders/${identifiers.purchase_order_id}/transition/`) && response.request().method() === "POST",
      );
      await row.getByRole("button", { name: "Submit purchase order" }).click();
      const submitted = record((await json(await submitResponsePromise)).purchase_order);
      expect(submitted).toMatchObject({
        status: "Submitted",
        approval_required: true,
        approved_by_id: null,
      });
      await expect(row.getByText("Submitted", { exact: true })).toBeVisible();

      const csrf = (await page.context().cookies()).find((cookie) => cookie.name === "csrftoken");
      expect(csrf, "CSRF cookie for an authenticated mutation").toBeTruthy();
      const forbidden = await page.context().request.post(
        `/api/v1/purchasing/purchase-orders/${identifiers.purchase_order_id}/transition/`,
        {
          headers: {
            "Idempotency-Key": "36cff864-dd88-4e20-83ed-44116ed7a301",
            "X-CSRFToken": csrf!.value,
          },
          data: { target: "Approved", status: "Approved" },
        },
      );
      expect(forbidden.status()).toBe(403);
      expect(await forbidden.json()).toMatchObject({ error: { code: "separation_of_duties" } });
    });

    await test.step("a separate purchasing approver approves and the buyer sends the order", async () => {
      await signOut(page);
      const approvingUser = await loginAs(page, approver);
      identifiers.approver_id = id(approvingUser);
      expect(approvingUser.roles).toEqual(expect.arrayContaining(["purchasing_manager"]));

      await page.goto("/purchase-orders");
      const row = purchaseOrderRow(page, String(identifiers.purchase_order_number), "E2E Brake Supply");
      await expect(row.getByText("Submitted", { exact: true })).toBeVisible();
      const approveResponsePromise = page.waitForResponse(
        (response) => response.url().includes(`/purchase-orders/${identifiers.purchase_order_id}/transition/`) && response.request().method() === "POST",
      );
      await row.getByRole("button", { name: "Approve purchase order" }).click();
      const approved = record((await json(await approveResponsePromise)).purchase_order);
      expect(approved).toMatchObject({
        status: "Approved",
        approval_required: true,
        approved_by_id: identifiers.approver_id,
      });
      await expect(row.getByText("Approved", { exact: true })).toBeVisible();

      await signOut(page);
      await loginAs(page, buyer);
      await page.goto("/purchase-orders");
      const buyerRow = purchaseOrderRow(page, String(identifiers.purchase_order_number), "E2E Brake Supply");
      const sentResponsePromise = page.waitForResponse(
        (response) => response.url().includes(`/purchase-orders/${identifiers.purchase_order_id}/transition/`) && response.request().method() === "POST",
      );
      await buyerRow.getByRole("button", { name: /send purchase order|mark sent/i }).click();
      const sent = record((await json(await sentResponsePromise)).purchase_order);
      expect(sent).toMatchObject({ status: "Sent", approval_required: true });
      await expect(buyerRow.getByText("Sent", { exact: true })).toBeVisible();

      await signOut(page);
      const receivingUser = await loginAs(page, receiver);
      identifiers.receiver_id = id(receivingUser);
      expect(receivingUser.roles).toEqual(expect.arrayContaining(["parts_clerk"]));
      await page.goto("/purchase-orders");
      await expect(page.getByRole("heading", { name: "Purchase orders", exact: true, level: 1 })).toBeVisible();
      await expect(page.getByRole("heading", { name: "Create purchase order", exact: true })).toHaveCount(0);
      const receivingRow = purchaseOrderRow(
        page,
        String(identifiers.purchase_order_number),
        "E2E Brake Supply",
      );
      await expect(receivingRow.getByText("Sent", { exact: true })).toBeVisible();
      await expect(receivingRow.getByRole("button", { name: "Receive items" })).toBeVisible();
    });

    await test.step("partial receiving changes stock once and leaves a visible backorder", async () => {
      const row = purchaseOrderRow(page, String(identifiers.purchase_order_number), "E2E Brake Supply");
      await row.getByRole("button", { name: "Receive items" }).click();
      const form = page.locator("section.panel").filter({
        has: page.getByRole("heading", { name: `Receive ${identifiers.purchase_order_number}` }),
      });
      await form.getByRole("combobox", { name: "Purchase order line", exact: true }).selectOption(String(identifiers.purchase_order_line_id));
      await form.getByRole("combobox", { name: "Receiving bin", exact: true }).selectOption(String(identifiers.bin_id));
      await form.getByLabel("Quantity received").fill("2");
      const receiptResponsePromise = page.waitForResponse(
        (response) => response.url().endsWith("/api/v1/purchasing/receipts/") && response.request().method() === "POST",
      );
      await form.getByRole("button", { name: "Post partial receipt" }).click();
      const receiptResponse = await receiptResponsePromise;
      const firstReceipt = record((await json(receiptResponse)).receipt);
      identifiers.first_receipt_id = id(firstReceipt);
      identifiers.first_receipt_number = firstReceipt.number;
      expect(firstReceipt.received_by_id).toBe(identifiers.receiver_id);
      const firstLine = record((firstReceipt.lines as unknown[])[0]);
      identifiers.first_stock_transaction_id = firstLine.stock_transaction_id;
      expect(firstReceipt.status).toBe("Posted");
      await expect(
        page.getByText("Receipt posted. Only the quantity received was added to stock.", { exact: true }),
      ).toBeVisible();

      const headers = await receiptResponse.request().allHeaders();
      const duplicate = await page.context().request.post("/api/v1/purchasing/receipts/", {
        headers: {
          "Content-Type": headers["content-type"],
          "Idempotency-Key": headers["idempotency-key"],
          "X-CSRFToken": headers["x-csrftoken"],
        },
        data: receiptResponse.request().postData()!,
      });
      const duplicateReceipt = record((await json(duplicate)).receipt);
      expect(id(duplicateReceipt)).toBe(identifiers.first_receipt_id);

      const receipts = list(
        await json(await page.context().request.get("/api/v1/purchasing/receipts/")),
        "receipts",
      ).filter((receipt) => receipt.purchase_order_id === identifiers.purchase_order_id);
      expect(receipts).toHaveLength(1);

      const order = list(
        await json(await page.context().request.get("/api/v1/purchasing/purchase-orders/")),
        "purchase_orders",
      ).find((candidate) => candidate.id === identifiers.purchase_order_id);
      expect(order).toBeTruthy();
      expect(order).toMatchObject({ status: "PartiallyReceived" });
      expect(record((order!.lines as unknown[])[0])).toMatchObject({
        quantity_received: "2.000",
        quantity_remaining: "4.000",
      });
      await expect(form.getByRole("combobox", { name: "Purchase order line", exact: true })).toContainText(
        /BRK-PAD-22 · 4\.000 remaining/,
      );

      const stock = await stockFor(page, String(identifiers.part_id), String(identifiers.bin_id));
      expect(quantity(stock.quantity_on_hand)).toBe(quantity(identifiers.opening_on_hand) + 2);
      expect(quantity(stock.quantity_reserved)).toBe(quantity(identifiers.opening_reserved));
      expect(quantity(stock.available_quantity)).toBe(
        quantity(identifiers.opening_on_hand) - quantity(identifiers.opening_reserved) + 2,
      );
    });

    await test.step("the remaining receipt closes the backorder and reversal appends compensation", async () => {
      const form = page.locator("section.panel").filter({
        has: page.getByRole("heading", { name: `Receive ${identifiers.purchase_order_number}` }),
      });
      await form.getByRole("combobox", { name: "Purchase order line", exact: true }).selectOption(String(identifiers.purchase_order_line_id));
      await form.getByRole("combobox", { name: "Receiving bin", exact: true }).selectOption(String(identifiers.bin_id));
      await form.getByLabel("Quantity received").fill("4");
      const receiptResponsePromise = page.waitForResponse(
        (response) => response.url().endsWith("/api/v1/purchasing/receipts/") && response.request().method() === "POST",
      );
      await form.getByRole("button", { name: "Post partial receipt" }).click();
      const finalReceipt = record((await json(await receiptResponsePromise)).receipt);
      identifiers.final_receipt_id = id(finalReceipt);
      identifiers.final_receipt_number = finalReceipt.number;
      expect(finalReceipt.received_by_id).toBe(identifiers.receiver_id);
      const finalLine = record((finalReceipt.lines as unknown[])[0]);
      identifiers.final_stock_transaction_id = finalLine.stock_transaction_id;

      await expect(
        purchaseOrderRow(page, String(identifiers.purchase_order_number), "E2E Brake Supply")
          .getByText("Received", { exact: true }),
      ).toBeVisible();
      let stock = await stockFor(page, String(identifiers.part_id), String(identifiers.bin_id));
      expect(quantity(stock.quantity_on_hand)).toBe(quantity(identifiers.opening_on_hand) + 6);

      const originalReceiptRow = receiptRow(
        page,
        String(identifiers.final_receipt_number),
        String(identifiers.purchase_order_number),
      );
      page.once("dialog", async (dialog) => {
        expect(dialog.type()).toBe("prompt");
        await dialog.accept("Carrier delivered this carton to the wrong shop");
      });
      const reversalResponsePromise = page.waitForResponse(
        (response) => response.url().includes(`/receipts/${identifiers.final_receipt_id}/reverse/`) && response.request().method() === "POST",
      );
      await originalReceiptRow.getByRole("button", { name: "Reverse receipt" }).click();
      const reversal = record((await json(await reversalResponsePromise)).receipt);
      identifiers.reversal_receipt_id = id(reversal);
      identifiers.reversal_receipt_number = reversal.number;
      const reversalLine = record((reversal.lines as unknown[])[0]);
      identifiers.reversal_stock_transaction_id = reversalLine.stock_transaction_id;
      expect(reversal).toMatchObject({
        status: "Posted",
        reversal_of_id: identifiers.final_receipt_id,
        received_by_id: identifiers.receiver_id,
      });
      expect(reversalLine).toMatchObject({ quantity: "-4.000" });
      await expect(
        page.getByText("Receipt reversed with compensating stock transactions.", { exact: true }),
      ).toBeVisible();

      await page.reload();
      await expect(
        purchaseOrderRow(page, String(identifiers.purchase_order_number), "E2E Brake Supply")
          .getByText("PartiallyReceived", { exact: true }),
      ).toBeVisible();
      await expect(
        receiptRow(page, String(identifiers.final_receipt_number), String(identifiers.purchase_order_number))
          .getByText("Reversed", { exact: true }),
      ).toBeVisible();
      await expect(
        receiptRow(page, String(identifiers.reversal_receipt_number), String(identifiers.purchase_order_number))
          .getByText("Posted", { exact: true }),
      ).toBeVisible();

      stock = await stockFor(page, String(identifiers.part_id), String(identifiers.bin_id));
      expect(quantity(stock.quantity_on_hand)).toBe(quantity(identifiers.opening_on_hand) + 2);
      const order = list(
        await json(await page.context().request.get("/api/v1/purchasing/purchase-orders/")),
        "purchase_orders",
      ).find((candidate) => candidate.id === identifiers.purchase_order_id);
      expect(order).toMatchObject({ status: "PartiallyReceived" });
      expect(record((order!.lines as unknown[])[0]).quantity_remaining).toBe("4.000");

      const durableReceipts = list(
        await json(await page.context().request.get("/api/v1/purchasing/receipts/")),
        "receipts",
      );
      expect(durableReceipts.find((receipt) => receipt.id === identifiers.final_receipt_id)).toMatchObject({
        status: "Reversed",
        received_by_id: identifiers.receiver_id,
        reversed_by_id: identifiers.receiver_id,
      });
      expect(durableReceipts.find((receipt) => receipt.id === identifiers.reversal_receipt_id)).toMatchObject({
        status: "Posted",
        received_by_id: identifiers.receiver_id,
        reversal_of_id: identifiers.final_receipt_id,
      });

      const history = await json(
        await page.context().request.get(`/api/v1/inventory/parts/${identifiers.part_id}/history/`),
      );
      const transactions = list(history, "transactions");
      const firstTransaction = transactions.find((row) => row.id === identifiers.first_stock_transaction_id);
      const finalTransaction = transactions.find((row) => row.id === identifiers.final_stock_transaction_id);
      const reversalTransaction = transactions.find((row) => row.id === identifiers.reversal_stock_transaction_id);
      expect(firstTransaction).toMatchObject({ type: "RECEIPT", quantity: "2.000" });
      expect(finalTransaction).toMatchObject({ type: "RECEIPT", quantity: "4.000" });
      expect(reversalTransaction).toMatchObject({
        type: "REVERSAL",
        quantity: "-4.000",
        original_transaction_id: identifiers.final_stock_transaction_id,
      });
    });

    await test.step("a fleet manager can trace the preserved records and actors in audit history", async () => {
      await signOut(page);
      await loginAs(page, auditor);
      await page.goto("/audit");
      await expect(page.getByRole("heading", { name: "Audit history" })).toBeVisible();
      await page.getByLabel("Resource type").fill("Receipt");
      await expect(
        page.getByText(`Receipt · ${identifiers.final_receipt_id}`, { exact: true }).first(),
      ).toBeVisible();
      await expect(page.getByText("receipt.reversed", { exact: true }).first()).toBeVisible();
      await expect(page.getByText(receiver, { exact: true }).first()).toBeVisible();

      const receiptEvents = list(
        await json(
          await page.context().request.get(
            `/api/v1/audit-events/?resource_type=Receipt&resource_id=${identifiers.final_receipt_id}`,
          ),
        ),
        "events",
      );
      expect(receiptEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({ action: "receipt.posted", actor: receiver, new_state: "Posted" }),
        expect.objectContaining({
          action: "receipt.reversed",
          actor: receiver,
          previous_state: "Posted",
          new_state: "Reversed",
        }),
      ]));

      const orderEvents = list(
        await json(
          await page.context().request.get(
            `/api/v1/audit-events/?resource_type=PurchaseOrder&resource_id=${identifiers.purchase_order_id}`,
          ),
        ),
        "events",
      );
      expect(orderEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({ action: "purchase_order.created", actor: buyer, new_state: "Draft" }),
        expect.objectContaining({ action: "purchase_order.submitted", actor: buyer, new_state: "Submitted" }),
        expect.objectContaining({ action: "purchase_order.approved", actor: approver, new_state: "Approved" }),
        expect.objectContaining({ action: "purchase_order.sent", actor: buyer, new_state: "Sent" }),
        expect.objectContaining({
          action: "purchase_order.receipt_status_changed",
          actor: receiver,
          previous_state: "Sent",
          new_state: "PartiallyReceived",
        }),
        expect.objectContaining({
          action: "purchase_order.receipt_status_changed",
          actor: receiver,
          previous_state: "PartiallyReceived",
          new_state: "Received",
        }),
        expect.objectContaining({
          action: "purchase_order.receipt_status_changed",
          actor: receiver,
          previous_state: "Received",
          new_state: "PartiallyReceived",
        }),
      ]));
      diagnostics.assertClean();
    });
  } finally {
    await testInfo.attach("purchasing-test-identifiers", {
      body: Buffer.from(JSON.stringify(identifiers, null, 2)),
      contentType: "application/json",
    });
    await diagnostics.attach(testInfo);
  }
});
