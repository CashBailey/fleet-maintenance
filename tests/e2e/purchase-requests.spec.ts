import { createHash } from "node:crypto";

import { expect, type Locator, type Page, test } from "@playwright/test";

import { collectBrowserDiagnostics } from "./helpers";

const password = process.env.E2E_PASSWORD ?? "DemoPass123!";
const clerk = "parts.clerk@example.com";
const buyer = "purchasing.manager@example.com";
const auditor = process.env.E2E_USERNAME ?? "fleet.manager@example.com";
const otherTenantManager = "other.manager@example.com";

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

function rows(value: unknown, key: string): Json[] {
  const found = record(value)[key];
  expect(Array.isArray(found)).toBe(true);
  return (found as unknown[]).map(record);
}

function id(value: Json): string {
  expect(value.id).toEqual(expect.any(String));
  return String(value.id);
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
  const user = record((await json(await responsePromise)).user);
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  return user;
}

async function signOut(page: Page): Promise<void> {
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/logout/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign out" }).click();
  expect((await responsePromise).status()).toBe(200);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
}

async function csrf(page: Page): Promise<string> {
  const response = await page.context().request.get("/api/v1/auth/csrf/");
  return String((await json(response)).csrf_token);
}

async function post(page: Page, path: string, data: Json, key: string): Promise<JsonResponse> {
  return page.context().request.post(path, {
    headers: { "Idempotency-Key": key, "X-CSRFToken": await csrf(page) },
    data,
  });
}

function operationKey(scope: string, label: string): string {
  const value = createHash("sha256").update(`${scope}:${label}`).digest("hex");
  return `${value.slice(0, 8)}-${value.slice(8, 12)}-4${value.slice(13, 16)}-8${value.slice(17, 20)}-${value.slice(20, 32)}`;
}

function requestRow(page: Page, reason: string): Locator {
  return page.locator("article.workflow-row").filter({ hasText: reason });
}

async function openRequests(page: Page): Promise<void> {
  await page.goto("/purchase-orders");
  await page.getByRole("tab", { name: "Purchase requests" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Purchase requests", exact: true })).toBeVisible();
}

async function createRequest(
  page: Page,
  partId: string,
  quantity: string,
  reason: string,
  neededBy: string,
): Promise<Json> {
  await page.getByRole("combobox", { name: "Part", exact: true }).selectOption(partId);
  await page.getByLabel("Quantity requested").fill(quantity);
  await page.getByLabel("Reason").fill(reason);
  await page.getByLabel("Needed by").fill(neededBy);
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/purchasing/purchase-requests/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Create purchase request" }).click();
  const created = record((await json(await responsePromise)).purchase_request);
  await expect(requestRow(page, reason).getByText("Submitted", { exact: true })).toBeVisible();
  return created;
}

test("purchase requests require independent approval and retain their purchase-order source", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  const diagnostics = collectBrowserDiagnostics(page);
  const scope = createHash("sha256")
    .update(`${process.env.E2E_RUN_ID ?? "manual"}:${testInfo.testId}`)
    .digest("hex")
    .slice(0, 10);
  const identifiers: Json = {
    primary_reason: `E2E seasonal oil replenishment ${scope}`,
    rejected_reason: `E2E duplicate brake stock request ${scope}`,
    cancelled_reason: `E2E filter request entered for wrong location ${scope}`,
  };

  try {
    await test.step("a parts clerk creates isolated requests and can cancel but not approve", async () => {
      const clerkUser = await loginAs(page, clerk);
      identifiers.clerk_id = id(clerkUser);
      expect(clerkUser.roles).toEqual(expect.arrayContaining(["parts_clerk"]));
      await openRequests(page);
      await expect(page.getByRole("button", { name: "Create purchase order" })).toHaveCount(0);

      const bootstrap = await json(await page.context().request.get("/api/v1/bootstrap/"));
      const parts = rows(bootstrap, "parts");
      const oil = parts.find((part) => part.number === "OIL-15W40");
      const brake = parts.find((part) => part.number === "BRK-PAD-22");
      const filter = parts.find((part) => part.number === "FIL-1001");
      expect(oil, "seeded oil part").toBeTruthy();
      expect(brake, "seeded brake part").toBeTruthy();
      expect(filter, "seeded filter part").toBeTruthy();
      identifiers.oil_part_id = id(oil!);

      const primary = await createRequest(
        page,
        id(oil!),
        "18",
        String(identifiers.primary_reason),
        "2026-12-15",
      );
      identifiers.primary_request_id = id(primary);
      expect(primary).toMatchObject({
        status: "Submitted",
        requested_by_id: identifiers.clerk_id,
        part_number: "OIL-15W40",
        quantity: "18.000",
      });

      const rejected = await createRequest(
        page,
        id(brake!),
        "3",
        String(identifiers.rejected_reason),
        "2026-12-16",
      );
      identifiers.rejected_request_id = id(rejected);
      const cancelled = await createRequest(
        page,
        id(filter!),
        "2",
        String(identifiers.cancelled_reason),
        "2026-12-17",
      );
      identifiers.cancelled_request_id = id(cancelled);

      const primaryRow = requestRow(page, String(identifiers.primary_reason));
      await expect(primaryRow.getByRole("button", { name: "Approve request" })).toHaveCount(0);
      await expect(primaryRow.getByRole("button", { name: "Reject request" })).toHaveCount(0);
      const forbidden = await post(
        page,
        `/api/v1/purchasing/purchase-requests/${identifiers.primary_request_id}/transition/`,
        { target: "Approved" },
        operationKey(scope, "forbidden-approval"),
      );
      expect(forbidden.status()).toBe(403);
      expect(await forbidden.json()).toMatchObject({ error: { code: "permission_denied" } });

      page.once("dialog", async (dialog) => {
        expect(dialog.type()).toBe("prompt");
        await dialog.accept("Requested for the wrong stock location");
      });
      const cancelResponsePromise = page.waitForResponse(
        (response) => response.url().includes(`/purchase-requests/${identifiers.cancelled_request_id}/transition/`) && response.request().method() === "POST",
      );
      await requestRow(page, String(identifiers.cancelled_reason))
        .getByRole("button", { name: "Cancel request" })
        .click();
      const cancelledResult = record((await json(await cancelResponsePromise)).purchase_request);
      expect(cancelledResult.status).toBe("Cancelled");
      await expect(
        requestRow(page, String(identifiers.cancelled_reason)).getByText("Cancelled", { exact: true }),
      ).toBeVisible();
    });

    await test.step("a purchasing manager approves one request and rejects another with a reason", async () => {
      await signOut(page);
      const buyerUser = await loginAs(page, buyer);
      identifiers.buyer_id = id(buyerUser);
      await openRequests(page);

      const primaryRow = requestRow(page, String(identifiers.primary_reason));
      const approveResponsePromise = page.waitForResponse(
        (response) => response.url().includes(`/purchase-requests/${identifiers.primary_request_id}/transition/`) && response.request().method() === "POST",
      );
      await primaryRow.getByRole("button", { name: "Approve request" }).click();
      const approved = record((await json(await approveResponsePromise)).purchase_request);
      expect(approved).toMatchObject({
        status: "Approved",
        approved_by_id: identifiers.buyer_id,
      });
      await expect(primaryRow.getByText("Approved", { exact: true })).toBeVisible();

      const noReason = await post(
        page,
        `/api/v1/purchasing/purchase-requests/${identifiers.rejected_request_id}/transition/`,
        { target: "Rejected", reason: "" },
        operationKey(scope, "reason-required"),
      );
      expect(noReason.status()).toBe(400);
      expect(await noReason.json()).toMatchObject({ error: { code: "reason_required" } });

      page.once("dialog", async (dialog) => {
        expect(dialog.type()).toBe("prompt");
        await dialog.accept("Existing order already covers this demand");
      });
      const rejectResponsePromise = page.waitForResponse(
        (response) => response.url().includes(`/purchase-requests/${identifiers.rejected_request_id}/transition/`) && response.request().method() === "POST",
      );
      await requestRow(page, String(identifiers.rejected_reason))
        .getByRole("button", { name: "Reject request" })
        .click();
      const rejected = record((await json(await rejectResponsePromise)).purchase_request);
      expect(rejected.status).toBe("Rejected");
      await expect(
        requestRow(page, String(identifiers.rejected_reason)).getByText("Rejected", { exact: true }),
      ).toBeVisible();
    });

    await test.step("the approved request preloads and links a purchase order through the UI", async () => {
      const primaryRow = requestRow(page, String(identifiers.primary_reason));
      await primaryRow.getByRole("button", { name: "Create purchase order" }).click();
      await expect(page.getByRole("heading", { level: 1, name: "Purchase orders", exact: true })).toBeVisible();
      await expect(page.getByRole("combobox", { name: "Source purchase request", exact: true })).toHaveValue(
        String(identifiers.primary_request_id),
      );
      await expect(page.getByRole("combobox", { name: "Part", exact: true })).toHaveValue(String(identifiers.oil_part_id));
      await expect(page.getByLabel("Quantity ordered")).toHaveValue("18.000");
      await page.getByRole("combobox", { name: "Vendor", exact: true }).selectOption({ label: "NAPA Heavy Duty" });
      await page.getByLabel("Unit cost").fill("6.25");
      await page.getByLabel("Notes").fill("Created from approved E2E seasonal oil request");

      const orderResponsePromise = page.waitForResponse(
        (response) => response.url().endsWith("/api/v1/purchasing/purchase-orders/") && response.request().method() === "POST",
      );
      await page.getByRole("button", { name: "Create purchase order" }).click();
      const order = record((await json(await orderResponsePromise)).purchase_order);
      identifiers.purchase_order_id = id(order);
      identifiers.purchase_order_number = order.number;
      const line = record((order.lines as unknown[])[0]);
      expect(order.status).toBe("Draft");
      expect(line).toMatchObject({
        part_number: "OIL-15W40",
        quantity_ordered: "18.000",
        purchase_request_id: identifiers.primary_request_id,
      });
      await expect(
        page.getByText("Purchase order created and linked to the approved purchase request.", { exact: true }),
      ).toBeVisible();

      await page.getByRole("tab", { name: "Purchase requests" }).click();
      const convertedRow = requestRow(page, String(identifiers.primary_reason));
      await expect(convertedRow.getByText("Converted", { exact: true })).toBeVisible();
      await expect(convertedRow.getByText("Converted to", { exact: false })).toBeVisible();
      await convertedRow.getByRole("button", { name: String(identifiers.purchase_order_number) }).click();
      await expect(page.getByRole("heading", { level: 1, name: "Purchase orders", exact: true })).toBeVisible();
      await expect(page.getByText(String(identifiers.purchase_order_number), { exact: false }).first()).toBeVisible();

      await page.reload();
      const durableRequests = rows(
        await json(await page.context().request.get("/api/v1/purchasing/purchase-requests/")),
        "purchase_requests",
      );
      expect(durableRequests.find((request) => request.id === identifiers.primary_request_id)).toMatchObject({
        status: "Converted",
        approved_by_id: identifiers.buyer_id,
      });
      expect(durableRequests.find((request) => request.id === identifiers.rejected_request_id)).toMatchObject({ status: "Rejected" });
      expect(durableRequests.find((request) => request.id === identifiers.cancelled_request_id)).toMatchObject({ status: "Cancelled" });
      const durableOrder = rows(
        await json(await page.context().request.get("/api/v1/purchasing/purchase-orders/")),
        "purchase_orders",
      ).find((candidate) => candidate.id === identifiers.purchase_order_id);
      expect(durableOrder, "purchase order linked to the request").toBeTruthy();
      expect(record((durableOrder!.lines as unknown[])[0]).purchase_request_id).toBe(
        identifiers.primary_request_id,
      );
    });

    await test.step("audit and tenant boundaries preserve the complete trace", async () => {
      await signOut(page);
      await loginAs(page, auditor);
      await page.goto("/audit");
      await page.getByLabel("Resource type").fill("PurchaseRequest");
      await expect(
        page.getByText(`PurchaseRequest · ${identifiers.primary_request_id}`, { exact: true }).first(),
      ).toBeVisible();

      const primaryEvents = rows(
        await json(await page.context().request.get(
          `/api/v1/audit-events/?resource_type=PurchaseRequest&resource_id=${identifiers.primary_request_id}`,
        )),
        "events",
      );
      expect(primaryEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({ action: "purchase_request.submitted", actor: clerk, new_state: "Submitted" }),
        expect.objectContaining({ action: "purchase_request.approved", actor: buyer, new_state: "Approved" }),
        expect.objectContaining({
          action: "purchase_request.converted",
          actor: buyer,
          previous_state: "Approved",
          new_state: "Converted",
          context: { purchase_order_id: identifiers.purchase_order_id },
        }),
      ]));
      const rejectedEvents = rows(
        await json(await page.context().request.get(
          `/api/v1/audit-events/?resource_type=PurchaseRequest&resource_id=${identifiers.rejected_request_id}`,
        )),
        "events",
      );
      expect(rejectedEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({
          action: "purchase_request.rejected",
          actor: buyer,
          context: { reason: "Existing order already covers this demand" },
        }),
      ]));
      const cancelledEvents = rows(
        await json(await page.context().request.get(
          `/api/v1/audit-events/?resource_type=PurchaseRequest&resource_id=${identifiers.cancelled_request_id}`,
        )),
        "events",
      );
      expect(cancelledEvents).toEqual(expect.arrayContaining([
        expect.objectContaining({
          action: "purchase_request.cancelled",
          actor: clerk,
          context: { reason: "Requested for the wrong stock location" },
        }),
      ]));

      await signOut(page);
      await loginAs(page, otherTenantManager);
      await openRequests(page);
      await expect(page.getByText(String(identifiers.primary_reason), { exact: false })).toHaveCount(0);
      const otherTenantRequests = rows(
        await json(await page.context().request.get("/api/v1/purchasing/purchase-requests/")),
        "purchase_requests",
      );
      expect(otherTenantRequests.map((request) => request.id)).not.toContain(
        identifiers.primary_request_id,
      );
      diagnostics.assertClean();
    });
  } finally {
    await testInfo.attach("purchase-request-test-identifiers", {
      body: Buffer.from(JSON.stringify(identifiers, null, 2)),
      contentType: "application/json",
    });
    await diagnostics.attach(testInfo);
  }
});
