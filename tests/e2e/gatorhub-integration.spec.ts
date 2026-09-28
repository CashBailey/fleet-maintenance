import { createHmac, randomUUID } from "node:crypto";

import { expect, type Page, type Response as PageResponse, test } from "@playwright/test";

import { collectBrowserDiagnostics } from "./helpers";

const password = process.env.E2E_PASSWORD ?? "DemoPass123!";
const mfaKey = Buffer.from("48656c6c6f21deadbeef", "hex");

interface Asset {
  id: string;
  external_id: string;
  make: string;
  model: string;
  source_system: string;
  unit_number: string;
  vin: string;
}

interface ExternalAssetResponse {
  asset: Asset;
  created: boolean;
  deep_link: string;
  schedule_link: string;
}

interface MeterReading {
  external_id: string;
  id: string;
  provenance: Record<string, unknown>;
  quality: string;
  source: string;
  value: string;
}

interface Meter {
  current_value: string;
  id: string;
  readings: MeterReading[];
}

interface AuditEvent {
  action: string;
  actor: string | null;
  correlation_id: string;
  source: string;
}

function totp(): string {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30_000)));
  const digest = createHmac("sha1", mfaKey).update(counter).digest();
  const offset = digest.at(-1)! & 0x0f;
  return ((digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000).toString().padStart(6, "0");
}

async function submitLogin(page: Page): Promise<PageResponse> {
  const response = page.waitForResponse(
    (candidate) => candidate.url().endsWith("/api/v1/auth/login/") && candidate.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  return response;
}

async function login(page: Page, username: string, mfa = false, path = "/"): Promise<void> {
  await page.goto(path, { waitUntil: "domcontentloaded" });
  await page.getByLabel(/^Username/).fill(username);
  await page.getByLabel(/^Password/).fill(password);
  let response = await submitLogin(page);
  if (mfa) {
    expect(response.status(), await response.text()).toBe(401);
    await page.getByLabel(/^One-time code/).fill(totp());
    response = await submitLogin(page);
  }
  expect(response.status(), await response.text()).toBe(200);
  await expect(page.locator("main")).toBeVisible();
}

async function csrf(page: Page): Promise<string> {
  const response = await page.context().request.get("/api/v1/auth/csrf/");
  expect(response.status(), await response.text()).toBe(200);
  return String((await response.json()).csrf_token);
}

test("GatorHub can upsert one truck and one meter reading through the supported boundary", async ({ page, request }, testInfo) => {
  test.setTimeout(120_000);
  const diagnostics = collectBrowserDiagnostics(page);
  try {
    await login(page, "system.admin@example.com", true);

    const usersResponse = await page.context().request.get("/api/v1/users/?role=integration_admin");
    expect(usersResponse.status(), await usersResponse.text()).toBe(200);
    const users = (await usersResponse.json() as { users: Array<{ id: string; username: string }> }).users;
    const integrationUser = users.find(({ username }) => username === "integration.admin@example.com");
    expect(integrationUser, "deterministic integration administrator").toBeDefined();

    const tokenResponse = await page.context().request.post("/api/v1/api-tokens/", {
      headers: { "X-CSRFToken": await csrf(page), "Idempotency-Key": randomUUID() },
      data: {
        user_id: integrationUser!.id,
        name: `E2E GatorHub asset sync ${randomUUID()}`,
        scopes: ["assets.view", "assets.sync"],
        expires_at: new Date(Date.now() + 60 * 60 * 1_000).toISOString(),
      },
    });
    expect(tokenResponse.status(), await tokenResponse.text()).toBe(201);
    const issuedToken = await tokenResponse.json() as {
      api_token: { scopes: string[] };
      token?: string;
    };
    expect(issuedToken.api_token.scopes).toEqual(["assets.sync", "assets.view"]);
    const token = String(issuedToken.token ?? "");
    expect(token).toMatch(/^flt_[0-9a-f]+_[A-Za-z0-9_-]+$/);

    const runId = randomUUID();
    const externalId = `vehicle-${runId}`;
    const unitNumber = `GH-${runId.slice(0, 8)}`.toUpperCase();
    const vin = `1GH${runId.replaceAll("-", "").slice(0, 14)}`.toUpperCase();
    const assetUrl = `/api/v1/assets/external/gatorhub/${externalId}/`;
    const bearerHeaders = { Authorization: `Bearer ${token}` };
    const createKey = randomUUID();
    const createResponse = await request.put(assetUrl, {
      headers: { ...bearerHeaders, "Idempotency-Key": createKey },
      data: {
        asset_type: "Truck",
        asset_type_category: "vehicle",
        unit_number: unitNumber,
        vin,
        year: 2024,
        make: "Kenworth",
        model: "T880",
        source_details: { license_plate: "E2E-001", vehicle_type: "vacuum_truck" },
      },
    });
    expect(createResponse.status(), await createResponse.text()).toBe(201);
    const created = await createResponse.json() as ExternalAssetResponse;
    expect(created).toMatchObject({
      created: true,
      asset: { external_id: externalId, source_system: "gatorhub", unit_number: unitNumber },
      deep_link: `/assets/${created.asset.id}`,
      schedule_link: `/schedule?asset_id=${created.asset.id}`,
    });

    const updateKey = randomUUID();
    const updateResponse = await request.put(assetUrl, {
      headers: { ...bearerHeaders, "Idempotency-Key": updateKey },
      data: {
        unit_number: unitNumber,
        make: "Kenworth",
        model: "T880 E2E",
        source_details: { license_plate: "E2E-002" },
      },
    });
    expect(updateResponse.status(), await updateResponse.text()).toBe(200);
    const updated = await updateResponse.json() as ExternalAssetResponse;
    expect(updated).toMatchObject({
      created: false,
      deep_link: created.deep_link,
      schedule_link: created.schedule_link,
      asset: { id: created.asset.id, model: "T880 E2E", unit_number: unitNumber },
    });

    const observedAt = new Date().toISOString();
    const meterPayload = {
      external_id: `dvir-${runId}`,
      kind: "odometer",
      name: "Odometer",
      unit: "mi",
      value: "12345.6",
      observed_at: observedAt,
    };
    const meterUrl = `${assetUrl}meters/`;
    const meterResponse = await request.post(meterUrl, {
      headers: { ...bearerHeaders, "Idempotency-Key": randomUUID() },
      data: meterPayload,
    });
    const replayResponse = await request.post(meterUrl, {
      headers: { ...bearerHeaders, "Idempotency-Key": randomUUID() },
      data: meterPayload,
    });
    expect(meterResponse.status(), await meterResponse.text()).toBe(201);
    expect(replayResponse.status(), await replayResponse.text()).toBe(201);
    const firstReading = await meterResponse.json() as { meter: Meter; reading: MeterReading };
    const replayedReading = await replayResponse.json() as { meter: Meter; reading: MeterReading };
    expect(replayedReading.reading.id).toBe(firstReading.reading.id);
    expect(firstReading.reading).toMatchObject({
      external_id: meterPayload.external_id,
      provenance: { entered_via: "external_asset_api", source_system: "gatorhub" },
      quality: "accepted",
      source: "gatorhub",
      value: "12345.600",
    });

    const lookupResponse = await request.get(assetUrl, { headers: bearerHeaders });
    expect(lookupResponse.status(), await lookupResponse.text()).toBe(200);
    expect(await lookupResponse.json()).toMatchObject({
      asset: { id: created.asset.id, model: "T880 E2E", unit_number: unitNumber },
      deep_link: created.deep_link,
    });

    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Sign in", exact: true })).toBeVisible();
    await login(page, "fleet.manager@example.com", false, created.schedule_link);
    await expect(page.getByRole("heading", { level: 1, name: "Schedule", exact: true })).toBeVisible();
    const assetSelect = page.getByRole("combobox", { name: "Asset", exact: true });
    await expect(assetSelect).toHaveValue(created.asset.id);
    await expect(assetSelect.locator("option:checked")).toHaveText(unitNumber);

    const servicePackage = page.getByRole("combobox", { name: "Service package", exact: true });
    await expect.poll(() => servicePackage.locator("option").count()).toBeGreaterThan(1);
    await servicePackage.selectOption({ index: 1 });
    const servicePackageId = await servicePackage.inputValue();
    expect(servicePackageId).not.toBe("");
    const planName = `GatorHub PM ${runId.slice(0, 8)}`;
    const lastCompletedDate = "2026-08-15";
    const expectedCompletedAt = await page.evaluate(
      (date) => new Date(`${date}T00:00`).toISOString(),
      lastCompletedDate,
    );
    await page.getByRole("textbox", { name: "Plan name", exact: true }).fill(planName);
    await page.getByRole("combobox", { name: "Trigger", exact: true }).selectOption("date");
    await page.getByRole("textbox", { name: /^Last completed date/ }).fill(lastCompletedDate);
    await page.getByRole("spinbutton", { name: "Interval", exact: true }).fill("90");
    await page.getByRole("spinbutton", { name: "Due soon threshold", exact: true }).fill("14");
    await page.getByRole("spinbutton", { name: "Grace", exact: true }).fill("7");
    await page.getByRole("combobox", { name: "Reset from", exact: true }).selectOption("scheduled");
    const [planRequest, planResponse] = await Promise.all([
      page.waitForRequest(
        (candidate) => candidate.url().endsWith("/api/v1/maintenance/plans/") && candidate.method() === "POST",
      ),
      page.waitForResponse(
        (candidate) => candidate.url().endsWith("/api/v1/maintenance/plans/") && candidate.request().method() === "POST",
      ),
      page.getByRole("button", { name: "Create plan", exact: true }).click(),
    ]);
    expect(planResponse.status(), await planResponse.text()).toBe(201);
    expect(planRequest.postDataJSON()).toMatchObject({
      asset_id: created.asset.id,
      service_package_id: servicePackageId,
      name: planName,
      triggers: [{
        kind: "date",
        interval: "90",
        grace: "7",
        due_soon_threshold: "14",
        reset_rule: "scheduled",
        last_completed_at: expectedCompletedAt,
      }],
    });
    const plan = (await planResponse.json() as {
      plan: { asset_id: string; triggers: Array<Record<string, unknown>> };
    }).plan;
    expect(plan.asset_id).toBe(created.asset.id);
    expect(plan.triggers).toHaveLength(1);
    expect(plan.triggers[0]).toMatchObject({
      kind: "date",
      interval: "90.00",
      grace: "7.00",
      due_soon_threshold: "14.00",
      reset_rule: "scheduled",
    });
    expect(new Date(String(plan.triggers[0].last_completed_at)).toISOString()).toBe(expectedCompletedAt);
    const planRow = page.locator(".workflow-row").filter({ hasText: planName });
    await expect(planRow).toHaveCount(1);
    await expect(planRow).toContainText(unitNumber);

    await page.goto(created.deep_link, { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(new RegExp(`${created.deep_link.replaceAll("/", "\\/")}$`));
    await expect(page.getByRole("heading", { level: 1, name: unitNumber, exact: true })).toBeVisible();
    await expect(page.getByRole("definition").filter({ hasText: vin })).toBeVisible();
    const meterHistory = page.locator("section.panel").filter({
      has: page.getByRole("heading", { level: 2, name: "Meter history", exact: true }),
    });
    await expect(meterHistory.locator("tbody tr")).toHaveCount(1);
    await expect(meterHistory.locator("tbody tr")).toContainText("12345.600 mi");
    await expect(meterHistory.locator("tbody tr")).toContainText("gatorhub");
    await expect(meterHistory.locator("tbody tr")).toContainText("accepted");

    const assetsResponse = await page.context().request.get(`/api/v1/assets/?q=${encodeURIComponent(unitNumber)}`);
    expect(assetsResponse.status(), await assetsResponse.text()).toBe(200);
    const matchingAssets = (await assetsResponse.json() as { assets: Asset[] }).assets
      .filter(({ unit_number }) => unit_number === unitNumber);
    expect(matchingAssets).toHaveLength(1);
    expect(matchingAssets[0].id).toBe(created.asset.id);

    const metersResponse = await page.context().request.get(`/api/v1/assets/${created.asset.id}/meters/`);
    expect(metersResponse.status(), await metersResponse.text()).toBe(200);
    const meters = (await metersResponse.json() as { meters: Meter[] }).meters;
    expect(meters).toHaveLength(1);
    expect(meters[0].readings).toHaveLength(1);
    expect(meters[0].readings[0].id).toBe(firstReading.reading.id);
    expect(meters[0].current_value).toBe("12345.600");

    const assetAuditsResponse = await page.context().request.get(
      `/api/v1/audit-events/?resource_type=Asset&resource_id=${created.asset.id}`,
    );
    expect(assetAuditsResponse.status(), await assetAuditsResponse.text()).toBe(200);
    const assetAudits = (await assetAuditsResponse.json() as { events: AuditEvent[] }).events
      .filter(({ action }) => action === "asset.external_upserted");
    expect(assetAudits).toHaveLength(2);
    expect(assetAudits).toEqual(expect.arrayContaining([
      expect.objectContaining({ actor: "integration.admin@example.com", correlation_id: updateKey, source: "gatorhub" }),
      expect.objectContaining({ actor: "integration.admin@example.com", correlation_id: createKey, source: "gatorhub" }),
    ]));

    const readingAuditsResponse = await page.context().request.get(
      `/api/v1/audit-events/?resource_type=MeterReading&resource_id=${firstReading.reading.id}`,
    );
    expect(readingAuditsResponse.status(), await readingAuditsResponse.text()).toBe(200);
    expect((await readingAuditsResponse.json() as { events: AuditEvent[] }).events).toEqual([
      expect.objectContaining({ action: "meter.reading_accepted", actor: "integration.admin@example.com", source: "gatorhub" }),
    ]);

    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});
