import { createHash, createHmac } from "node:crypto";

import { expect, type APIRequestContext, type Page, type TestInfo, test } from "@playwright/test";

import { collectBrowserDiagnostics, e2eUser, loginThroughUi } from "./helpers";

const integrationUser = "integration.admin@example.com";
const password = process.env.E2E_PASSWORD ?? "DemoPass123!";
const mfaSecret = "JBSWY3DPEHPK3PXP";
const ingestPath = "/api/v1/integrations/telematics/autopi/v1/messages/";

interface Reading {
  id: string;
  value: string;
  source: string;
  quality: string;
  reason: string;
  provenance: { telematics_message_id?: string };
}

interface Meter {
  id: string;
  kind: string;
  current_reading: Reading;
  current_value: string;
  readings: Reading[];
}

interface Payload {
  schemaVersion: string;
  messageId: string;
  organizationId: string;
  deviceId: string;
  observedAt: string;
  sentAt: string;
  sequence: number;
  source: string;
  type: string;
  values: { odometer: { value: number; unit: string } };
}

interface AuditEvent {
  action: string;
  resource_type: string;
  resource_id: string;
  actor: string | null;
  context: Record<string, unknown>;
  source: string;
  occurred_at: string;
}

function runScope(testInfo: TestInfo): string {
  return createHash("sha256")
    .update(
      `${process.env.E2E_RUN_ID ?? "manual"}:${testInfo.testId}:${testInfo.parallelIndex}:${testInfo.repeatEachIndex}:${testInfo.retry}`,
    )
    .digest("hex")
    .slice(0, 12);
}

function decodeBase32(value: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  const bytes: number[] = [];
  let bits = 0;
  let buffer = 0;
  for (const character of value.replace(/=+$/, "").toUpperCase()) {
    const digit = alphabet.indexOf(character);
    if (digit < 0) throw new Error("Invalid base32 MFA fixture");
    buffer = (buffer << 5) | digit;
    bits += 5;
    if (bits >= 8) {
      bits -= 8;
      bytes.push((buffer >>> bits) & 0xff);
      buffer &= (1 << bits) - 1;
    }
  }
  return Buffer.from(bytes);
}

function currentTotp(secret: string): string {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30_000)));
  const digest = createHmac("sha1", decodeBase32(secret)).update(counter).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const code = (digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000;
  return code.toString().padStart(6, "0");
}

async function loginAsIntegrationAdmin(page: Page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.getByLabel(/username|email/i).fill(integrationUser);
  await page.getByLabel(/password/i).fill(password);

  const challengePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/login/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: /sign in|log in/i }).click();
  expect((await challengePromise).status()).toBe(401);

  await expect(page.getByLabel("One-time code")).toBeVisible();
  await page.getByLabel("One-time code").fill(currentTotp(mfaSecret));
  const loginPromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/login/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: /sign in|log in/i }).click();
  const response = await loginPromise;
  expect(response.status(), await response.text()).toBe(200);
  await expect(page.getByRole("heading", { name: "Integration health" })).toBeVisible();
}

async function logout(page: Page) {
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/logout/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign out" }).click();
  expect((await responsePromise).status()).toBe(200);
  await expect(page.getByLabel(/username|email/i)).toBeVisible();
}

async function odometer(request: APIRequestContext, assetId: string): Promise<Meter> {
  const response = await request.get(`/api/v1/assets/${assetId}/meters/`);
  expect(response.status(), await response.text()).toBe(200);
  const body = (await response.json()) as { meters: Meter[] };
  const meter = body.meters.find((candidate) => candidate.kind === "odometer");
  expect(meter, "asset odometer").toBeDefined();
  return meter!;
}

function payload(
  prefix: string,
  message: string,
  organizationId: string,
  deviceId: string,
  observedAt: string,
  value = 1000,
  unit = "mi",
  sequence = 1,
): Payload {
  return {
    schemaVersion: "1.0",
    messageId: `${prefix}-${message}`,
    organizationId,
    deviceId,
    observedAt,
    sentAt: observedAt,
    sequence,
    source: "autopi",
    type: "telemetry",
    values: { odometer: { value, unit } },
  };
}

async function ingest(request: APIRequestContext, token: string, body: Payload) {
  return request.post(ingestPath, { data: body, headers: { "X-Device-Token": token } });
}

async function expectAudit(
  request: APIRequestContext,
  resourceType: string,
  resourceId: string,
  action: string,
  expected: Partial<AuditEvent>,
): Promise<AuditEvent> {
  const response = await request.get(
    `/api/v1/audit-events/?resource_type=${encodeURIComponent(resourceType)}&resource_id=${encodeURIComponent(resourceId)}`,
  );
  expect(response.status(), await response.text()).toBe(200);
  const body = (await response.json()) as { events: AuditEvent[] };
  expect(Array.isArray(body.events), "audit response events").toBe(true);
  const matches = body.events.filter((event) => event.action === action);
  expect(matches, `${action} audit events for ${resourceType} ${resourceId}`).toHaveLength(1);
  const event = matches[0];
  expect(event).toMatchObject({ action, resource_type: resourceType, resource_id: resourceId, ...expected });
  expect(Number.isNaN(Date.parse(event.occurred_at)), `${action} audit timestamp must be ISO 8601`).toBe(false);
  return event;
}

test("AutoPi device registration, meter validation, history, and manual fallback", async ({ page }, testInfo) => {
  let diagnostics: ReturnType<typeof collectBrowserDiagnostics> | undefined;
  const suffix = runScope(testInfo);
  const unitNumber = `AUTO-${suffix}`.toUpperCase();
  const serialNumber = `E2E-AUTOPI-${suffix}`;
  const externalId = `e2e-autopi-${suffix}`;
  const messagePrefix = `e2e-${suffix}`;

  try {
    // A fleet manager establishes an isolated asset and its manual meter through the real UI.
    await loginThroughUi(page);
    await page.goto("/assets");
    await page.getByText("New asset", { exact: true }).click();
    await page.getByLabel("Unit number").fill(unitNumber);
    await page.getByLabel("Truck type").fill("Truck");
    const assetResponsePromise = page.waitForResponse(
      (response) => response.url().endsWith("/api/v1/assets/") && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Create asset" }).click();
    const assetResponse = await assetResponsePromise;
    expect(assetResponse.status(), await assetResponse.text()).toBe(201);
    const assetBody = (await assetResponse.json()) as { asset: { id: string } };
    const assetId = assetBody.asset.id;
    await expect(page.getByRole("link", { name: unitNumber, exact: true })).toBeVisible();

    await page.goto(`/assets/${assetId}`);
    await page.getByLabel("New meter type").selectOption("odometer");
    await page.getByLabel("Unit").selectOption("mi");
    await page.getByLabel("Reading").fill("1000");
    const baselinePromise = page.waitForResponse(
      (response) => response.url().endsWith(`/api/v1/assets/${assetId}/meters/`) && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Record meter" }).click();
    const baselineResponse = await baselinePromise;
    expect(baselineResponse.status(), await baselineResponse.text()).toBe(201);
    const baselineBody = (await baselineResponse.json()) as { meter: Meter; reading: Reading };
    expect(baselineBody.reading).toMatchObject({ value: "1000.000", source: "manual", quality: "accepted" });
    const baselineReadingId = baselineBody.reading.id;
    const meterId = baselineBody.meter.id;
    await expect(page.getByRole("row").filter({ hasText: "1000.000 mi" }).filter({ hasText: /manual/i })).toBeVisible();
    await logout(page);

    // Registration and the time-bounded association are principal UI actions by an MFA-protected admin.
    await loginAsIntegrationAdmin(page);
    // The intentional MFA challenge is a 401, so page-console diagnostics begin after it succeeds.
    diagnostics = collectBrowserDiagnostics(page);
    const meResponse = await page.context().request.get("/api/v1/auth/me/");
    expect(meResponse.status()).toBe(200);
    const me = (await meResponse.json()) as { user: { organization_id: string; roles: string[] } };
    expect(me.user.roles).toContain("integration_admin");

    await page.goto("/integrations");
    await page.getByLabel("Device name").fill(`AutoPi ${unitNumber}`);
    await page.getByLabel("Model").fill("TMU CM4");
    await page.getByLabel("Serial number").fill(serialNumber);
    await page.getByLabel("External device ID").fill(externalId);
    const registrationPromise = page.waitForResponse(
      (response) => response.url().endsWith("/api/v1/integrations/devices/") && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Register device" }).click();
    const registrationResponse = await registrationPromise;
    expect(registrationResponse.status(), await registrationResponse.text()).toBe(201);
    const registration = (await registrationResponse.json()) as { device: { id: string; token: string } };
    expect(registration.device.token).toMatch(/^fdev_[A-Za-z0-9_-]+$/);
    await expect(page.getByRole("status").filter({ hasText: registration.device.token })).toBeVisible();

    const deviceSelect = page.getByRole("combobox", { name: "Device", exact: true });
    await expect(deviceSelect.locator(`option[value="${registration.device.id}"]`)).toHaveCount(1);
    await deviceSelect.selectOption(registration.device.id);
    await page.getByRole("combobox", { name: "Asset", exact: true }).selectOption(assetId);
    const associationPromise = page.waitForResponse(
      (response) => response.url().includes(`/api/v1/integrations/devices/${registration.device.id}/associate/`) && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Assign device" }).click();
    const associationResponse = await associationPromise;
    expect(associationResponse.status(), await associationResponse.text()).toBe(201);
    const association = (await associationResponse.json()) as {
      association: { id: string; asset_id: string; effective_from: string };
    };
    expect(association.association.asset_id).toBe(assetId);
    await expect(page.getByRole("status").filter({ hasText: "Device assignment recorded" })).toBeVisible();

    const associationTime = Date.parse(association.association.effective_from);
    expect(associationTime).not.toBeNaN();
    const olderAt = new Date(associationTime + 10).toISOString();
    const validAt = new Date(associationTime + 20).toISOString();
    const decreasingAt = new Date(associationTime + 30).toISOString();
    const implausibleAt = new Date(associationTime + 40).toISOString();
    const validPayload = payload(messagePrefix, "valid", me.user.organization_id, externalId, validAt);

    // The first payload enters via the UI fixture form, which calls the public production adapter.
    await page.getByText("Submit recorded AutoPi payload", { exact: true }).click();
    await page.getByLabel("Device ingestion token").fill(registration.device.token);
    await page.getByLabel("Payload JSON").fill(JSON.stringify(validPayload));
    const validPromise = page.waitForResponse(
      (response) => response.url().endsWith(ingestPath) && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Ingest meter payload" }).click();
    const validResponse = await validPromise;
    expect(validResponse.status(), await validResponse.text()).toBe(202);
    const validBody = (await validResponse.json()) as {
      message: { id: string; status: string; duplicate: boolean; normalized_events: Array<{ quality: string }> };
    };
    expect(validBody.message).toMatchObject({ status: "accepted", duplicate: false });
    expect(validBody.message.normalized_events).toEqual([expect.objectContaining({ quality: "accepted" })]);
    await expect(page.getByRole("status")).toContainText("AutoPi message accepted");

    const afterValid = await odometer(page.context().request, assetId);
    expect(afterValid.id).toBe(meterId);
    expect(afterValid.current_value).toBe("1000.000");
    const validReading = afterValid.readings.find(
      (reading) => reading.provenance.telematics_message_id === validBody.message.id,
    );
    expect(validReading).toMatchObject({ source: "autopi", quality: "accepted", value: "1000.000" });
    const validReadingId = validReading!.id;
    const countAfterValid = afterValid.readings.length;

    // Replaying the identical public message returns the original fact and appends nothing.
    const replayResponse = await ingest(page.context().request, registration.device.token, validPayload);
    expect(replayResponse.status(), await replayResponse.text()).toBe(200);
    expect(await replayResponse.json()).toMatchObject({
      message: { id: validBody.message.id, status: "accepted", duplicate: true, normalized_events: [] },
    });
    expect((await odometer(page.context().request, assetId)).readings).toHaveLength(countAfterValid);

    // An older valid observation is retained, while the later observation remains the projection.
    const latePayload = payload(messagePrefix, "out-of-order", me.user.organization_id, externalId, olderAt, 1000, "mi", 2);
    const lateResponse = await ingest(page.context().request, registration.device.token, latePayload);
    expect(lateResponse.status(), await lateResponse.text()).toBe(202);
    const lateBody = (await lateResponse.json()) as { message: { id: string; normalized_events: Array<{ quality: string }> } };
    expect(lateBody.message.normalized_events).toEqual([expect.objectContaining({ quality: "accepted" })]);
    const afterLate = await odometer(page.context().request, assetId);
    expect(afterLate.current_reading.id).toBe(validReadingId);
    expect(afterLate.readings).toContainEqual(
      expect.objectContaining({
        source: "autopi",
        quality: "accepted",
        provenance: expect.objectContaining({ telematics_message_id: lateBody.message.id }),
      }),
    );

    // Decreasing and physically impossible values stay in history as suspect and never replace current.
    const decreasingResponse = await ingest(
      page.context().request,
      registration.device.token,
      payload(messagePrefix, "decreasing", me.user.organization_id, externalId, decreasingAt, 999, "mi", 3),
    );
    expect(decreasingResponse.status(), await decreasingResponse.text()).toBe(202);
    const decreasingBody = (await decreasingResponse.json()) as {
      message: { id: string; normalized_events: Array<{ quality: string; reason: string }> };
    };
    expect(decreasingBody).toMatchObject({
      message: { normalized_events: [expect.objectContaining({ quality: "suspect", reason: expect.stringContaining("decreases") })] },
    });

    const implausibleResponse = await ingest(
      page.context().request,
      registration.device.token,
      payload(messagePrefix, "implausible", me.user.organization_id, externalId, implausibleAt, 10_000_001, "mi", 4),
    );
    expect(implausibleResponse.status(), await implausibleResponse.text()).toBe(202);
    const implausibleBody = (await implausibleResponse.json()) as {
      message: { id: string; normalized_events: Array<{ quality: string; reason: string }> };
    };
    expect(implausibleBody).toMatchObject({
      message: { normalized_events: [expect.objectContaining({ quality: "suspect", reason: expect.stringContaining("configured maximum") })] },
    });
    const afterSuspect = await odometer(page.context().request, assetId);
    expect(afterSuspect.current_reading.id).toBe(validReadingId);
    expect(afterSuspect.readings.filter((reading) => reading.quality === "suspect")).toHaveLength(2);
    const decreasingReading = afterSuspect.readings.find(
      (reading) => reading.provenance.telematics_message_id === decreasingBody.message.id,
    );
    const implausibleReading = afterSuspect.readings.find(
      (reading) => reading.provenance.telematics_message_id === implausibleBody.message.id,
    );
    expect(decreasingReading).toMatchObject({ quality: "suspect", reason: expect.stringContaining("decreases") });
    expect(implausibleReading).toMatchObject({ quality: "suspect", reason: expect.stringContaining("configured maximum") });
    const normalizedCount = afterSuspect.readings.length;

    // Identity, unit, and timestamp failures are explicit, durable, and create no meter reading.
    const wrongIdentity = payload(messagePrefix, "wrong-identity", me.user.organization_id, "other-device", validAt);
    const identityResponse = await ingest(page.context().request, registration.device.token, wrongIdentity);
    expect(identityResponse.status(), await identityResponse.text()).toBe(403);
    const identityBody = (await identityResponse.json()) as { error: { code: string; details: { message_id: string } } };
    expect(identityBody).toMatchObject({ error: { code: "identity_mismatch" } });

    const invalidUnit = payload(messagePrefix, "invalid-unit", me.user.organization_id, externalId, validAt, 1000, "liters");
    const unitResponse = await ingest(page.context().request, registration.device.token, invalidUnit);
    expect(unitResponse.status(), await unitResponse.text()).toBe(400);
    const unitBody = (await unitResponse.json()) as { error: { code: string; details: { message_id: string } } };
    expect(unitBody).toMatchObject({ error: { code: "unsupported_unit" } });

    const invalidTimestamp = payload(messagePrefix, "invalid-timezone", me.user.organization_id, externalId, "2026-09-03T12:00:00");
    const timestampResponse = await ingest(page.context().request, registration.device.token, invalidTimestamp);
    expect(timestampResponse.status(), await timestampResponse.text()).toBe(400);
    const timestampBody = (await timestampResponse.json()) as { error: { code: string; details: { message_id: string } } };
    expect(timestampBody).toMatchObject({ error: { code: "invalid_timestamp" } });

    const futureAt = new Date(Date.now() + 10 * 60_000).toISOString();
    const futureResponse = await ingest(
      page.context().request,
      registration.device.token,
      payload(messagePrefix, "future", me.user.organization_id, externalId, futureAt, 1000, "mi", 5),
    );
    expect(futureResponse.status(), await futureResponse.text()).toBe(422);
    const futureBody = (await futureResponse.json()) as { error: { code: string; details: { message_id: string } } };
    expect(futureBody).toMatchObject({ error: { code: "future_timestamp" } });
    expect((await odometer(page.context().request, assetId)).readings).toHaveLength(normalizedCount);

    const devicesResponse = await page.context().request.get("/api/v1/integrations/devices/");
    expect(devicesResponse.status(), await devicesResponse.text()).toBe(200);
    const devicesBody = (await devicesResponse.json()) as {
      devices: Array<Record<string, string | number | object | null>>;
    };
    const device = devicesBody.devices.find((row) => row.id === registration.device.id);
    expect(device).toMatchObject({
      external_id: externalId,
      message_count: 4,
      duplicate_count: 1,
      rejected_count: 3,
      quarantined_count: 3,
      asset: expect.objectContaining({ id: assetId, unit_number: unitNumber }),
    });

    await page.goto("/data-quality");
    await expect(page.getByText("reading decreases from prior accepted value", { exact: true })).toBeVisible();
    await expect(page.getByText("value exceeds configured maximum", { exact: true })).toBeVisible();

    // Telemetry failures do not remove the authorized manual meter workflow.
    await logout(page);
    await loginThroughUi(page);
    await page.goto(`/assets/${assetId}`);
    await expect(page.getByRole("heading", { name: "Record meter" })).toBeVisible();
    await page.getByLabel("Existing meter").selectOption(meterId);
    await page.getByLabel("Reading").fill("1000");
    const manualPromise = page.waitForResponse(
      (response) => response.url().endsWith(`/api/v1/assets/${assetId}/meters/`) && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Record meter" }).click();
    const manualResponse = await manualPromise;
    expect(manualResponse.status(), await manualResponse.text()).toBe(201);
    const manualBody = (await manualResponse.json()) as { reading: Reading };
    expect(manualBody).toMatchObject({
      reading: { source: "manual", quality: "accepted", value: "1000.000" },
    });
    const finalMeter = await odometer(page.context().request, assetId);
    expect(finalMeter.current_reading).toMatchObject({ source: "manual", quality: "accepted" });
    expect(finalMeter.readings).toHaveLength(normalizedCount + 1);
    await expect(
      page.getByRole("row").filter({ hasText: "1000.000 mi" }).filter({ hasText: /manual/i }).first(),
    ).toBeVisible();

    // Each required boundary leaves one attributable, timestamped, tenant-scoped audit fact.
    const auditRequest = page.context().request;
    await expectAudit(auditRequest, "Device", registration.device.id, "device.registered", {
      actor: integrationUser,
      source: "web",
      context: { provider: "autopi", serial_number: serialNumber },
    });
    await expectAudit(auditRequest, "DeviceAssetAssociation", association.association.id, "device.associated", {
      actor: integrationUser,
      source: "web",
      context: { device_id: registration.device.id, asset_id: assetId },
    });
    await expectAudit(auditRequest, "TelematicsMessage", validBody.message.id, "telematics.message_accepted", {
      actor: null,
      source: "autopi",
      context: { device_id: registration.device.id, asset_id: assetId, normalized_event_count: 1 },
    });
    await expectAudit(auditRequest, "MeterReading", validReadingId, "meter.reading_accepted", {
      actor: null,
      source: "autopi",
      context: { asset_id: assetId, meter_id: meterId, source: "autopi" },
    });
    for (const reading of [decreasingReading!, implausibleReading!]) {
      await expectAudit(auditRequest, "MeterReading", reading.id, "meter.reading_quarantined", {
        actor: null,
        source: "autopi",
        context: { asset_id: assetId, meter_id: meterId, source: "autopi" },
      });
    }
    await expectAudit(
      auditRequest,
      "TelematicsMessage",
      futureBody.error.details.message_id,
      "telematics.message_quarantined",
      { actor: null, source: "autopi", context: { device_id: registration.device.id } },
    );
    for (const messageId of [
      identityBody.error.details.message_id,
      unitBody.error.details.message_id,
      timestampBody.error.details.message_id,
    ]) {
      await expectAudit(auditRequest, "TelematicsMessage", messageId, "telematics.message_rejected", {
        actor: null,
        source: "autopi",
        context: { device_id: registration.device.id },
      });
    }
    await expectAudit(auditRequest, "MeterReading", manualBody.reading.id, "meter.reading_accepted", {
      actor: e2eUser.username,
      source: "manual",
      context: { asset_id: assetId, meter_id: meterId, source: "manual" },
    });
    await expectAudit(auditRequest, "MeterReading", baselineReadingId, "meter.reading_accepted", {
      actor: e2eUser.username,
      source: "manual",
      context: { asset_id: assetId, meter_id: meterId, source: "manual" },
    });

    diagnostics.assertClean();
  } finally {
    if (diagnostics) await diagnostics.attach(testInfo);
    else {
      await testInfo.attach("browser-diagnostics", {
        body: Buffer.from("The scenario failed before the expected MFA challenge completed; see trace and network artifacts."),
        contentType: "text/plain",
      });
    }
  }
});
