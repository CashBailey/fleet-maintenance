import { createHmac, randomUUID } from "node:crypto";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";

import { expect, type Page, test } from "@playwright/test";

import { collectBrowserDiagnostics } from "./helpers";

const password = process.env.E2E_PASSWORD ?? "DemoPass123!";
const deviceToken = process.env.E2E_DEVICE_TOKEN ?? "e2e-autopi-device-token";
const baseURL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:8088";
const mfaKey = Buffer.from("48656c6c6f21deadbeef", "hex");

function totp(): string {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30_000)));
  const digest = createHmac("sha1", mfaKey).update(counter).digest();
  const offset = digest.at(-1)! & 0x0f;
  return ((digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000).toString().padStart(6, "0");
}

async function submitLogin(page: Page) {
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/login/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  return responsePromise;
}

async function login(page: Page, username: string, mfa = false) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.getByLabel(/^Username/).fill(username);
  await page.getByLabel(/^Password/).fill(password);
  let response = await submitLogin(page);
  if (mfa) {
    expect(response.status(), await response.text()).toBe(401);
    await page.getByLabel(/^One-time code/).fill(totp());
    response = await submitLogin(page);
  }
  expect(response.status(), await response.text()).toBe(200);
}

async function csrf(page: Page): Promise<string> {
  const response = await page.context().request.get("/api/v1/auth/csrf/");
  expect(response.status(), await response.text()).toBe(200);
  return String((await response.json()).csrf_token);
}

test("webhook administration signs, retries, audits, and enforces tenant RBAC", async ({ browser, page }, testInfo) => {
  let receiverStatus = 503;
  const received: Array<{ body: Buffer; signature: string }> = [];
  const receiver = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      received.push({
        body: Buffer.concat(chunks),
        signature: String(request.headers["x-fleetline-signature"] ?? ""),
      });
      response.writeHead(receiverStatus).end();
    });
  });
  await new Promise<void>((resolve, reject) => {
    receiver.once("error", reject);
    receiver.listen(0, "127.0.0.1", resolve);
  });

  const otherContext = await browser.newContext({ baseURL });
  const otherPage = await otherContext.newPage();
  let diagnostics: ReturnType<typeof collectBrowserDiagnostics> | undefined;
  let otherDiagnostics: ReturnType<typeof collectBrowserDiagnostics> | undefined;
  try {
    await login(page, "integration.admin@example.com", true);
    diagnostics = collectBrowserDiagnostics(page);
    await expect(page.getByRole("heading", { name: "Integration health", exact: true })).toBeVisible();
    const meResponse = await page.context().request.get("/api/v1/auth/me/");
    expect(meResponse.status(), await meResponse.text()).toBe(200);
    const me = (await meResponse.json()) as { user: { organization_id: string } };

    const devicesResponse = await page.context().request.get("/api/v1/integrations/devices/");
    expect(devicesResponse.status(), await devicesResponse.text()).toBe(200);
    const devices = (await devicesResponse.json()) as {
      devices: Array<{ external_id: string }>;
    };
    const device = devices.devices.find(({ external_id }) => external_id === "autopi-demo-012");
    expect(device, "deterministic AutoPi seed device").toBeDefined();

    const suffix = randomUUID();
    const name = `E2E webhook ${suffix}`;
    const receiverURL = `http://127.0.0.1:${(receiver.address() as AddressInfo).port}/fleetline`;
    await page.goto("/administration");
    await expect(page.getByRole("heading", { name: "Administration", exact: true })).toBeVisible();
    await page.getByLabel(/^Webhook name/).fill(name);
    await page.getByLabel(/^Destination URL/).fill(receiverURL);
    await page.getByLabel(/^Event types/).fill("telematics.event_normalized");
    const createPromise = page.waitForResponse(
      (response) => response.url().endsWith("/api/v1/webhooks/") && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Create webhook", exact: true }).click();
    const created = await createPromise;
    expect(created.status(), await created.text()).toBe(201);
    const createdBody = (await created.json()) as { webhook: { id: string; signing_secret: string } };
    expect(createdBody.webhook.signing_secret.length).toBeGreaterThan(30);
    await expect(page.getByRole("status").filter({ hasText: createdBody.webhook.signing_secret })).toBeVisible();
    await expect(page.getByText(name, { exact: true })).toBeVisible();

    const subscriptions = await page.context().request.get("/api/v1/webhooks/");
    expect(subscriptions.status(), await subscriptions.text()).toBe(200);
    const subscriptionBody = (await subscriptions.json()) as { webhooks: Array<Record<string, unknown>> };
    const listed = subscriptionBody.webhooks.find(({ id }) => id === createdBody.webhook.id);
    expect(listed).toMatchObject({ name, url: receiverURL, event_types: ["telematics.event_normalized"] });
    expect(listed).not.toHaveProperty("signing_secret");

    const observedAt = new Date().toISOString();
    const ingest = await page.context().request.post(
      "/api/v1/integrations/telematics/autopi/v1/messages/",
      {
        headers: { "X-Device-Token": deviceToken },
        data: {
          schemaVersion: "1.0",
          messageId: `webhook-${suffix}`,
          organizationId: me.user.organization_id,
          deviceId: device!.external_id,
          observedAt,
          sentAt: observedAt,
          sequence: Number.parseInt(suffix.replaceAll("-", "").slice(0, 12), 16),
          source: "autopi",
          type: "diagnostics",
          values: {},
          diagnostics: [{ protocol: "j1939", spn: 98765, fmi: 1, occurrences: 1 }],
        },
      },
    );
    expect(ingest.status(), await ingest.text()).toBe(202);

    await expect.poll(() => received.length, { message: "signed webhook request received" }).toBeGreaterThan(0);
    expect(received[0].signature).toBe(
      `sha256=${createHmac("sha256", createdBody.webhook.signing_secret).update(received[0].body).digest("hex")}`,
    );
    expect(JSON.parse(received[0].body.toString())).toMatchObject({
      schema_version: "1.0",
      type: "telematics.event_normalized",
      organization_id: me.user.organization_id,
    });

    let delivery: { id: string; status: string; attempts: number } | undefined;
    await expect.poll(async () => {
      const response = await page.context().request.get("/api/v1/webhooks/deliveries/?status=retry");
      expect(response.status(), await response.text()).toBe(200);
      const body = (await response.json()) as {
        deliveries: Array<{ id: string; status: string; attempts: number; subscription: { id: string } }>;
      };
      delivery = body.deliveries.find((item) => item.subscription.id === createdBody.webhook.id);
      return delivery?.status;
    }, { message: "failed delivery enters the retry queue" }).toBe("retry");

    await page.getByLabel(/^Delivery status/).selectOption("dead");
    await expect(page.getByText(/dead-letter deliveries/)).toBeVisible();
    await page.getByLabel(/^Delivery status/).selectOption("retry");
    const row = page.locator(".workflow-row")
      .filter({ has: page.getByRole("button", { name: "Retry delivery", exact: true }) })
      .filter({ hasText: name });
    await expect(row).toContainText("retry", { ignoreCase: true });

    // Make the receiver healthy before the delivery becomes immediately eligible again.
    // Otherwise the worker can race the API response and schedule another backoff.
    receiverStatus = 204;
    const retryPromise = page.waitForResponse(
      (response) => response.url().includes(`/api/v1/webhooks/deliveries/${delivery!.id}/retry/`) && response.request().method() === "POST",
    );
    await row.getByRole("button", { name: "Retry delivery", exact: true }).click();
    const retried = await retryPromise;
    expect(retried.status(), await retried.text()).toBe(200);
    expect(await retried.json()).toMatchObject({ delivery: { id: delivery!.id, status: "retry", attempts: 0 } });
    await expect(page.getByRole("status").filter({ hasText: "Webhook delivery queued for retry." })).toBeVisible();

    await expect.poll(async () => {
      const response = await page.context().request.get("/api/v1/webhooks/deliveries/?status=delivered");
      const body = (await response.json()) as { deliveries: Array<{ id: string; status: string; response_status: number }> };
      return body.deliveries.find(({ id }) => id === delivery!.id);
    }, { message: "retried delivery reaches the local receiver" }).toMatchObject({
      id: delivery!.id,
      status: "delivered",
      response_status: 204,
    });
    await page.getByLabel(/^Delivery status/).selectOption("delivered");
    const deliveredRow = page.locator(".workflow-row").filter({ hasText: name }).filter({ hasText: "response 204" });
    await expect(deliveredRow).toContainText("delivered", { ignoreCase: true });
    await expect(deliveredRow).toContainText("response 204", { ignoreCase: true });

    const rotatePromise = page.waitForResponse(
      (response) => response.url().includes(`/api/v1/webhooks/${createdBody.webhook.id}/rotate-secret/`) && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: `Rotate signing secret for ${name}`, exact: true }).click();
    const rotated = await rotatePromise;
    expect(rotated.status(), await rotated.text()).toBe(200);
    const rotatedBody = await rotated.json() as { webhook: { signing_secret: string }; secret_recoverable: boolean };
    expect(rotatedBody.secret_recoverable).toBe(true);
    expect(rotatedBody.webhook.signing_secret).not.toBe(createdBody.webhook.signing_secret);
    await expect(page.getByRole("status").filter({ hasText: rotatedBody.webhook.signing_secret })).toBeVisible();

    const deactivatePromise = page.waitForResponse(
      (response) => response.url().includes(`/api/v1/webhooks/${createdBody.webhook.id}/status/`) && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: `Deactivate ${name}`, exact: true }).click();
    const deactivated = await deactivatePromise;
    expect(deactivated.status(), await deactivated.text()).toBe(200);
    expect(await deactivated.json()).toMatchObject({ webhook: { id: createdBody.webhook.id, active: false } });
    await expect(page.getByRole("button", { name: `Activate ${name}`, exact: true })).toBeVisible();

    const auditResponse = await page.context().request.get(
      `/api/v1/audit-events/?resource_type=WebhookDelivery&resource_id=${delivery!.id}`,
    );
    expect(auditResponse.status(), await auditResponse.text()).toBe(200);
    expect(await auditResponse.json()).toMatchObject({
      events: [expect.objectContaining({
        action: "webhook.delivery_retried",
        actor: "integration.admin@example.com",
        previous_state: "retry",
        new_state: "retry",
      })],
    });
    const subscriptionAudit = await page.context().request.get(
      `/api/v1/audit-events/?resource_type=WebhookSubscription&resource_id=${createdBody.webhook.id}`,
    );
    expect(subscriptionAudit.status(), await subscriptionAudit.text()).toBe(200);
    const subscriptionEvents = (await subscriptionAudit.json()).events as Array<{ action: string }>;
    expect(subscriptionEvents.map(({ action }) => action)).toEqual(expect.arrayContaining([
      "webhook.created",
      "webhook.secret_rotated",
      "webhook.status_changed",
    ]));

    await login(otherPage, "other.manager@example.com");
    otherDiagnostics = collectBrowserDiagnostics(otherPage);
    const forbiddenList = await otherContext.request.get("/api/v1/webhooks/deliveries/");
    expect(forbiddenList.status(), await forbiddenList.text()).toBe(403);
    const forbiddenRetry = await otherContext.request.post(
      `/api/v1/webhooks/deliveries/${delivery!.id}/retry/`,
      {
        headers: { "X-CSRFToken": await csrf(otherPage), "Idempotency-Key": randomUUID() },
        data: {},
      },
    );
    expect(forbiddenRetry.status(), await forbiddenRetry.text()).toBe(403);
    expect(await forbiddenRetry.json()).toMatchObject({ error: { code: "permission_denied" } });

    diagnostics.assertClean();
    otherDiagnostics.assertClean();
  } finally {
    if (diagnostics) await diagnostics.attach(testInfo);
    if (otherDiagnostics) await otherDiagnostics.attach(testInfo);
    await otherContext.close();
    receiver.closeAllConnections();
    await new Promise<void>((resolve) => receiver.close(() => resolve()));
  }
});
