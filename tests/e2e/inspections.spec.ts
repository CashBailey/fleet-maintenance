import { randomUUID } from "node:crypto";

import { expect, type Page, test } from "@playwright/test";

import { collectBrowserDiagnostics } from "./helpers";

type Json = Record<string, unknown>;

const password = process.env.E2E_PASSWORD ?? "DemoPass123!";

function object(value: unknown, label: string): Json {
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

async function loginAs(page: Page, username: string): Promise<void> {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
  await page.getByLabel(/^Username/).fill(username);
  await page.getByLabel(/^Password/).fill(password);
  const responsePromise = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/auth/login/") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBe(200);
  await expect(page.locator("main")).toBeVisible();
}

async function logout(page: Page): Promise<void> {
  const responsePromise = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/v1/auth/logout/") &&
      response.request().method() === "POST",
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

test("@maintenance a driver inspection failure creates an immutable finding and defect chain", async ({
  page,
}, testInfo) => {
  test.setTimeout(90_000);
  const diagnostics = collectBrowserDiagnostics(page);
  const scope = randomUUID();
  const note = `Air loss at left front brake chamber ${scope}`;
  const attachmentName = `inspection-${scope}.pdf`;
  const attachmentBody = `%PDF-1.4\nFleetline inspection evidence ${scope}\n%%EOF\n`;

  try {
    await loginAs(page, "driver@example.com");
    await page.getByRole("link", { name: "Inspect", exact: true }).click();
    await expect(
      page.getByRole("heading", { level: 1, name: "Inspections", exact: true }),
    ).toBeVisible();

    const asset = page.getByRole("combobox", { name: "Asset", exact: true });
    const assetOption = asset.locator("option").filter({ hasText: "TRK-012" });
    const assetId = await assetOption.getAttribute("value");
    expect(assetId, "the driver seed must include assigned asset TRK-012").toBeTruthy();
    await asset.selectOption(assetId!);
    await page
      .getByRole("combobox", { name: "Inspection template", exact: true })
      .selectOption({ label: "Daily Pre-Trip" });

    const brakeItem = page.getByRole("group", { name: "Brakes operate normally" });
    await expect(brakeItem).toBeVisible();
    await brakeItem.getByRole("radio", { name: "Needs attention", exact: true }).check();
    await brakeItem.getByPlaceholder("Optional note").fill(note);
    await page.getByLabel("Add photo or file", { exact: true }).setInputFiles({
      name: attachmentName,
      mimeType: "application/pdf",
      buffer: Buffer.from(attachmentBody),
    });
    await page
      .getByRole("checkbox", { name: /I confirm this inspection is complete and accurate/ })
      .check();

    const syncPromise = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/v1/offline/sync/") &&
        response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Submit inspection", exact: true }).click();
    await expect(page.getByRole("status").filter({ hasText: "Saved on this device" })).toBeVisible();
    const syncResponse = await syncPromise;
    const syncBody = object(await syncResponse.json(), "offline sync response");
    expect(syncResponse.status(), JSON.stringify(syncBody)).toBe(200);
    const syncItem = rows(syncBody, "results").find(
      (candidate) => candidate.status === "synced" && object(candidate.result, "result").status === "Submitted",
    );
    expect(syncItem, "inspection operation must synchronize").toBeTruthy();
    const inspection = object(syncItem!.result, "inspection result");
    const inspectionId = String(inspection.id);
    expect(inspectionId).toMatch(/^[0-9a-f-]{36}$/i);
    expect(inspection).toMatchObject({
      asset_id: assetId,
      status: "Submitted",
      template_name: "Daily Pre-Trip",
      template_version: 1,
      performed_by: "Drew Driver",
    });
    await expect(page.getByTestId("sync-status").first()).toContainText("Synchronized");

    await page.reload({ waitUntil: "domcontentloaded" });
    const history = page
      .getByRole("heading", { level: 2, name: "Inspection history", exact: true })
      .locator("..").locator("..")
      .locator(".workflow-row")
      .filter({ hasText: "TRK-012 · Daily Pre-Trip" });
    await expect(history.first()).toContainText("Submitted");

    const persisted = object(
      (await getJson(page, `/api/v1/maintenance/inspections/${inspectionId}/`)).inspection,
      "persisted inspection",
    );
    const response = rows(persisted, "responses").find(
      (candidate) => candidate.question_id === "brakes",
    );
    expect(response).toEqual(
      expect.objectContaining({ result: "fail", notes: note, safety_critical: true }),
    );
    const finding = rows(persisted, "findings").find(
      (candidate) => candidate.question_id === "brakes",
    );
    expect(finding).toEqual(
      expect.objectContaining({
        description: note,
        severity: "safety",
        safety_related: true,
        status: "Open",
        inspection_id: inspectionId,
      }),
    );
    expect(response?.finding_id).toBe(finding?.id);
    const defectId = String(finding?.defect_id ?? "");
    expect(defectId).toMatch(/^[0-9a-f-]{36}$/i);

    const defect = rows(await getJson(page, "/api/v1/maintenance/defects/"), "defects").find(
      (candidate) => candidate.id === defectId,
    );
    expect(defect).toEqual(
      expect.objectContaining({
        asset_id: assetId,
        inspection_finding_id: finding?.id,
        inspection_response_id: response?.id,
        category: "inspection",
        description: note,
        severity: "safety",
        safety_related: true,
        status: "Open",
      }),
    );

    const attachments = rows(
      await getJson(
        page,
        `/api/v1/attachments/?resource_type=Inspection&resource_id=${inspectionId}`,
      ),
      "attachments",
    );
    expect(attachments).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ name: attachmentName, content_type: "application/pdf" }),
      ]),
    );

    await logout(page);
    await loginAs(page, "supervisor@example.com");
    const inspectionAudits = rows(
      await getJson(
        page,
        `/api/v1/audit-events/?resource_type=Inspection&resource_id=${inspectionId}`,
      ),
      "events",
    );
    expect(inspectionAudits).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          action: "inspection.submitted",
          actor: "driver@example.com",
          new_state: "Submitted",
          source: "offline_sync",
        }),
      ]),
    );
    const findingAudits = rows(
      await getJson(
        page,
        `/api/v1/audit-events/?resource_type=InspectionFinding&resource_id=${String(finding?.id)}`,
      ),
      "events",
    );
    expect(findingAudits).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          action: "inspection_finding.created",
          actor: "driver@example.com",
          new_state: "Open",
        }),
      ]),
    );
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});
