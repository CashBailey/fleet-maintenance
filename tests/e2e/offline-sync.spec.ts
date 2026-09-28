import { randomUUID } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { chromium, expect, test, type BrowserContext, type Page, type TestInfo } from "@playwright/test";

const baseURL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:8088";
const password = process.env.E2E_PASSWORD ?? "DemoPass123!";
const offlineDatabase = "fleetline-field-v1";

type Json = Record<string, unknown>;

interface StoredOperation {
  operation_id: string;
  type: string;
  payload: Json;
  created_at: string;
  status: string;
  message?: string;
  user_id: string;
  organization_id: string;
  expires_at: string;
  offline_grant: string;
  blob_ids: string[];
}

interface StoredBlob {
  id: string;
  operation_id: string;
  name: string;
  type: string;
  size: number;
  body: string;
}

function watchPage(page: Page, log: string[]) {
  page.on("console", (message) => log.push(`console:${message.type()}: ${message.text()}`));
  page.on("pageerror", (error) => log.push(`pageerror: ${error.message}`));
  page.on("requestfailed", (request) => {
    log.push(`requestfailed: ${request.method()} ${request.url()} ${request.failure()?.errorText ?? "unknown"}`);
  });
  page.on("response", (response) => {
    if (response.status() >= 400) log.push(`response:${response.status()}: ${response.request().method()} ${response.url()}`);
  });
}

function assertOnlyExpectedOfflineFailures(log: string[]) {
  const expectedNetworkFailure = /ERR_INTERNET_DISCONNECTED|ERR_CONNECTION_RESET|ERR_FAILED|NS_ERROR_OFFLINE/i;
  const unexpected = log.filter((entry) => {
    if (!/^(?:console:error|pageerror|requestfailed|response:)/.test(entry)) return false;
    return !expectedNetworkFailure.test(entry);
  });
  expect(unexpected, "unexpected browser errors while exercising real offline controls").toEqual([]);
}

function visibleSyncStatus(page: Page) {
  return page.getByTestId("sync-status").filter({ visible: true });
}

async function attachDiagnostics(testInfo: TestInfo, log: string[]) {
  await testInfo.attach("offline-browser-diagnostics", {
    body: Buffer.from(log.length ? log.join("\n") : "No browser errors or failed requests."),
    contentType: "text/plain",
  });
}

async function loginAs(page: Page, username: string) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.getByLabel(/username|email/i).fill(username);
  await page.getByLabel(/password/i).fill(password);
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/login/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: /sign in|log in/i }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBeLessThan(300);
  await expect(page).not.toHaveURL(/\/login(?:[/?#]|$)/);
  await expect(page.locator("main")).toBeVisible();
}

async function ensureServiceWorkerControls(page: Page) {
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await expect.poll(() => page.evaluate(() => Boolean(navigator.serviceWorker.controller))).toBe(true);
}

async function readStore<T>(page: Page, storeName: "cache" | "outbox" | "blobs"): Promise<T[]> {
  return page.evaluate(
    ({ database, store }) => new Promise<T[]>((resolve, reject) => {
      const request = indexedDB.open(database);
      request.onerror = () => reject(request.error);
      request.onsuccess = () => {
        const db = request.result;
        const transaction = db.transaction(store, "readonly");
        const all = transaction.objectStore(store).getAll();
        all.onerror = () => reject(all.error);
        all.onsuccess = () => resolve(all.result as T[]);
        transaction.oncomplete = () => db.close();
        transaction.onerror = () => reject(transaction.error);
      };
    }),
    { database: offlineDatabase, store: storeName },
  );
}

async function putStore<T>(page: Page, storeName: "cache" | "outbox" | "blobs", rows: T[]): Promise<void> {
  await page.evaluate(
    ({ database, store, values }) => new Promise<void>((resolve, reject) => {
      const request = indexedDB.open(database);
      request.onerror = () => reject(request.error);
      request.onsuccess = () => {
        const db = request.result;
        const transaction = db.transaction(store, "readwrite");
        values.forEach((row) => transaction.objectStore(store).put(row));
        transaction.oncomplete = () => { db.close(); resolve(); };
        transaction.onerror = () => { db.close(); reject(transaction.error); };
      };
    }),
    { database: offlineDatabase, store: storeName, values: rows },
  );
}

async function deleteStoreRecord(page: Page, storeName: "outbox" | "blobs", key: string): Promise<void> {
  await page.evaluate(
    ({ database, store, recordKey }) => new Promise<void>((resolve, reject) => {
      const request = indexedDB.open(database);
      request.onerror = () => reject(request.error);
      request.onsuccess = () => {
        const db = request.result;
        const transaction = db.transaction(store, "readwrite");
        transaction.objectStore(store).delete(recordKey);
        transaction.oncomplete = () => { db.close(); resolve(); };
        transaction.onerror = () => { db.close(); reject(transaction.error); };
      };
    }),
    { database: offlineDatabase, store: storeName, recordKey: key },
  );
}

async function readStoredBlobs(page: Page): Promise<StoredBlob[]> {
  return page.evaluate(
    ({ database, store }) => new Promise<StoredBlob[]>((resolve, reject) => {
      const request = indexedDB.open(database);
      request.onerror = () => reject(request.error);
      request.onsuccess = () => {
        const db = request.result;
        const transaction = db.transaction(store, "readonly");
        const all = transaction.objectStore(store).getAll();
        all.onerror = () => reject(all.error);
        all.onsuccess = () => {
          void Promise.all((all.result as (Omit<StoredBlob, "size" | "body"> & { blob: Blob })[]).map(async (row) => ({
            id: row.id,
            operation_id: row.operation_id,
            name: row.name,
            type: row.type,
            size: row.blob.size,
            body: await row.blob.text(),
          }))).then(resolve, reject);
        };
        transaction.oncomplete = () => db.close();
        transaction.onerror = () => reject(transaction.error);
      };
    }),
    { database: offlineDatabase, store: "blobs" },
  );
}

async function storedOfflineIdentity(page: Page): Promise<Pick<StoredOperation, "user_id" | "organization_id" | "expires_at" | "offline_grant">> {
  await expect.poll(async () => (await readStore<StoredOperation & { key: string }>(page, "cache"))
    .some((row) => row.key === "bootstrap"), { message: "cached signed offline identity" }).toBe(true);
  const identity = (await readStore<StoredOperation & { key: string }>(page, "cache"))
    .find((row) => row.key === "bootstrap");
  expect(identity).toBeDefined();
  return identity!;
}

async function getJson<T>(context: BrowserContext, path: string): Promise<T> {
  const response = await context.request.get(path);
  expect(response.status(), await response.text()).toBeLessThan(300);
  return response.json() as Promise<T>;
}

async function mutateJson<T>(
  context: BrowserContext,
  method: "POST" | "PATCH",
  path: string,
  data: Json,
  idempotencyKey = randomUUID(),
  headers: Record<string, string> = {},
): Promise<T> {
  let csrf = (await context.cookies()).find(({ name }) => name === "csrftoken")?.value;
  if (!csrf) {
    const csrfResponse = await context.request.get("/api/v1/auth/csrf/");
    expect(csrfResponse.status(), await csrfResponse.text()).toBeLessThan(300);
    csrf = String((await csrfResponse.json() as { csrf_token?: string }).csrf_token ?? "");
  }
  const response = await context.request.fetch(path, {
    method,
    data,
    headers: { "X-CSRFToken": csrf, "Idempotency-Key": idempotencyKey, ...headers },
  });
  expect(response.status(), await response.text()).toBeLessThan(300);
  return response.json() as Promise<T>;
}

test("@offline an attachment-backed defect survives reload and a browser-profile restart, then syncs exactly once", async ({ browserName }, testInfo) => {
  test.setTimeout(120_000);
  expect(browserName, "the production E2E project uses Chromium/Google Chrome").toBe("chromium");
  const diagnostics: string[] = [];
  const runId = randomUUID();
  const description = `E2E offline defect ${runId}: brake hose chafing at left steer axle`;
  const fileBody = `Fleetline offline attachment proof\nrun_id=${runId}\nasset=TRK-012\narea=Brakes\n`;
  const profileDir = await mkdtemp(join(tmpdir(), "fleetline-offline-profile-"));
  let cleanupContext: BrowserContext | undefined;

  try {
    let fieldContext = await chromium.launchPersistentContext(profileDir, { channel: "chrome" });
    cleanupContext = fieldContext;
    let activePage = fieldContext.pages()[0] ?? await fieldContext.newPage();
    watchPage(activePage, diagnostics);
    await loginAs(activePage, "driver@example.com");
    await ensureServiceWorkerControls(activePage);
    await activePage.goto("/report-problem");
    await expect(activePage.getByRole("heading", { name: "Report a defect" })).toBeVisible();

    const before = await getJson<{ defects: Json[] }>(fieldContext, "/api/v1/maintenance/defects/");
    const beforeCount = before.defects.filter((defect) => defect.description === description).length;
    expect(beforeCount, "fresh offline defect fixture").toBe(0);

    const asset = activePage.getByLabel(/^Asset\b/);
    const assetValue = await asset.locator("option").filter({ hasText: "TRK-012" }).getAttribute("value");
    expect(assetValue).toBeTruthy();
    await asset.selectOption(assetValue!);
    await activePage.getByRole("button", { name: /Brakes/ }).click();
    await activePage.getByLabel(/^Describe it\b/).fill(description);
    await activePage.getByLabel("Add photo or file").setInputFiles({
      name: `offline-defect-proof-${runId}.txt`,
      mimeType: "text/plain",
      buffer: Buffer.from(fileBody),
    });

    await fieldContext.setOffline(true);
    await activePage.getByRole("button", { name: "Submit defect" }).click();
    await expect(activePage.getByText(/Saved on this device\. It will synchronize when a connection is available\./)).toBeVisible();
    await expect(visibleSyncStatus(activePage).getByTestId("outbox-count")).toContainText("1 change pending");

    await expect.poll(async () => (await readStore<StoredOperation>(activePage, "outbox")).length).toBe(1);
    const [queued] = await readStore<StoredOperation>(activePage, "outbox");
    expect(queued).toMatchObject({
      type: "defect.create",
      status: "waiting",
      payload: { description, category: "Brakes", safety_related: false },
    });
    expect(queued.blob_ids).toHaveLength(1);
    const [storedFile] = await readStoredBlobs(activePage);
    expect(storedFile).toMatchObject({
      id: queued.blob_ids[0],
      operation_id: queued.operation_id,
      name: `offline-defect-proof-${runId}.txt`,
      type: "text/plain",
      body: fileBody,
    });
    expect(storedFile.size).toBe(Buffer.byteLength(fileBody));

    await activePage.reload({ waitUntil: "domcontentloaded" });
    await expect(activePage.getByRole("heading", { name: "Report a defect" })).toBeVisible();
    expect(await readStore<StoredOperation>(activePage, "outbox")).toMatchObject([
      { operation_id: queued.operation_id, payload: { description } },
    ]);

    const cachedShells = await activePage.evaluate(async () => (await caches.keys()).sort());
    expect(cachedShells).toContain("fleetline-v2-shell");
    const sessionState = await fieldContext.storageState();
    expect(sessionState.cookies.some((cookie) => cookie.name === "fleetline_sessionid"), "authenticated session cookie captured for browser restart").toBe(true);
    await fieldContext.close();
    cleanupContext = undefined;

    fieldContext = await chromium.launchPersistentContext(profileDir, {
      channel: "chrome",
      offline: true,
    });
    cleanupContext = fieldContext;
    await fieldContext.addCookies(sessionState.cookies);
    activePage = fieldContext.pages()[0] ?? await fieldContext.newPage();
    watchPage(activePage, diagnostics);
    await activePage.goto("/report-problem", { waitUntil: "domcontentloaded" });
    await expect(activePage.getByRole("heading", { name: "Report a defect" })).toBeVisible();
    await ensureServiceWorkerControls(activePage);
    expect(await activePage.evaluate(async () => (await caches.keys()).sort())).toEqual(cachedShells);
    expect(await readStore<StoredOperation>(activePage, "outbox")).toMatchObject([
      { operation_id: queued.operation_id, payload: { description } },
    ]);
    expect(await readStoredBlobs(activePage)).toMatchObject([
      { id: queued.blob_ids[0], name: `offline-defect-proof-${runId}.txt`, body: fileBody },
    ]);

    let resolveCommittedAttachment!: (value: { status: number; body: { attachment: { id: string } }; key: string }) => void;
    let rejectCommittedAttachment!: (reason: unknown) => void;
    const committedAttachmentPromise = new Promise<{ status: number; body: { attachment: { id: string } }; key: string }>(
      (resolve, reject) => { resolveCommittedAttachment = resolve; rejectCommittedAttachment = reject; },
    );
    await activePage.route("**/api/v1/attachments/", async (route) => {
      try {
        const response = await route.fetch();
        const headers = await route.request().allHeaders();
        resolveCommittedAttachment({
          status: response.status(),
          body: await response.json() as { attachment: { id: string } },
          key: headers["idempotency-key"] ?? "",
        });
        await route.abort("connectionreset");
      } catch (error) {
        rejectCommittedAttachment(error);
        await route.abort("failed");
      }
    });

    await fieldContext.setOffline(false);
    await visibleSyncStatus(activePage).click();
    const committedAttachment = await committedAttachmentPromise;
    expect(committedAttachment.status).toBe(201);
    expect(committedAttachment.key).toBe(queued.blob_ids[0]);
    await expect.poll(async () => (await readStore<StoredOperation>(activePage, "outbox"))[0]?.status).toBe("waiting");
    expect(await readStoredBlobs(activePage)).toHaveLength(1);
    await activePage.unroute("**/api/v1/attachments/");

    const attachmentResponsePromise = activePage.waitForResponse(
      (response) => response.url().endsWith("/api/v1/attachments/") && response.request().method() === "POST",
    );
    const syncResponsePromise = activePage.waitForResponse(
      (response) => response.url().endsWith("/api/v1/offline/sync/") && response.request().method() === "POST",
    );
    await visibleSyncStatus(activePage).click();
    const attachmentResponse = await attachmentResponsePromise;
    expect(attachmentResponse.status(), await attachmentResponse.text()).toBe(201);
    const uploaded = await attachmentResponse.json() as { attachment: { id: string } };
    expect(uploaded.attachment.id).toBe(committedAttachment.body.attachment.id);
    expect((await attachmentResponse.request().allHeaders())["idempotency-key"]).toBe(committedAttachment.key);
    const syncResponse = await syncResponsePromise;
    expect(syncResponse.status(), await syncResponse.text()).toBe(200);

    const syncRequest = syncResponse.request().postDataJSON() as { operations: Json[] };
    const syncResult = await syncResponse.json() as {
      results: { operation_id: string; status: string; result: Json }[];
    };
    expect(syncRequest.operations).toHaveLength(1);
    expect(syncRequest.operations[0]).toMatchObject({
      operation_id: queued.operation_id,
      payload: { attachment_ids: [uploaded.attachment.id] },
    });
    expect(syncResult.results).toHaveLength(1);
    expect(syncResult.results[0]).toMatchObject({ operation_id: queued.operation_id, status: "synced" });
    const defectId = String(syncResult.results[0].result.id);

    await expect(visibleSyncStatus(activePage)).toContainText("Synchronized");
    await expect(visibleSyncStatus(activePage).getByTestId("outbox-count")).toContainText("All changes are up to date");
    await expect.poll(async () => (await readStore<StoredOperation>(activePage, "outbox")).length).toBe(0);
    expect(await readStoredBlobs(activePage)).toHaveLength(0);

    await activePage.goto("/defects", { waitUntil: "domcontentloaded" });
    await expect(activePage.getByText(description, { exact: true })).toBeVisible();
    await activePage.reload({ waitUntil: "domcontentloaded" });
    await expect(activePage.getByText(description, { exact: true })).toBeVisible();

    const after = await getJson<{ defects: Json[] }>(fieldContext, "/api/v1/maintenance/defects/");
    const matchingDefects = after.defects.filter((defect) => defect.description === description);
    expect(matchingDefects).toHaveLength(1);
    const createdDefect = matchingDefects.find((defect) => defect.id === defectId);
    expect(createdDefect).toMatchObject({
      id: defectId,
      asset: "TRK-012",
      category: "Brakes",
      severity: "medium",
      safety_related: false,
      status: "Open",
    });

    const attachments = await getJson<{ attachments: Json[] }>(
      fieldContext,
      `/api/v1/attachments/?resource_type=Defect&resource_id=${defectId}`,
    );
    expect(attachments.attachments).toHaveLength(1);
    expect(attachments.attachments[0]).toMatchObject({
      name: `offline-defect-proof-${runId}.txt`,
      content_type: "text/plain",
      size: Buffer.byteLength(fileBody),
    });
    const attachmentId = String(attachments.attachments[0].id);
    const download = await fieldContext.request.get(`/api/v1/attachments/${attachmentId}/download/`);
    expect(download.status(), await download.text()).toBe(200);
    expect((await download.body()).toString()).toBe(fileBody);

    const replay = await mutateJson<{
      results: { operation_id: string; status: string; result: Json }[];
    }>(fieldContext, "POST", "/api/v1/offline/sync/", syncRequest, randomUUID(), {
      "X-Offline-Grant": queued.offline_grant,
    });
    expect(replay.results[0]).toMatchObject({
      operation_id: queued.operation_id,
      status: "synced",
      result: { id: defectId },
    });
    const afterReplay = await getJson<{ defects: Json[] }>(fieldContext, "/api/v1/maintenance/defects/");
    expect(afterReplay.defects.filter((defect) => defect.description === description)).toHaveLength(1);
    const attachmentsAfterReplay = await getJson<{ attachments: Json[] }>(
      fieldContext,
      `/api/v1/attachments/?resource_type=Defect&resource_id=${defectId}`,
    );
    expect(attachmentsAfterReplay.attachments).toHaveLength(1);

    await testInfo.attach("offline-defect-state", {
      body: Buffer.from(JSON.stringify({ queued, committedAttachment, syncRequest, syncResult, defect: createdDefect, attachments }, null, 2)),
      contentType: "application/json",
    });
    assertOnlyExpectedOfflineFailures(diagnostics);
  } finally {
    await cleanupContext?.setOffline(false).catch(() => undefined);
    await cleanupContext?.close().catch(() => undefined);
    await rm(profileDir, { recursive: true, force: true }).catch(() => undefined);
    await attachDiagnostics(testInfo, diagnostics);
  }
});

test("@offline a server-side work-order change produces a readable conflict without losing the queued task update", async ({ page, context, browser }, testInfo) => {
  const diagnostics: string[] = [];
  const runId = randomUUID();
  const taskTitle = `Inspect offline conflict harness ${runId}`;
  const serverSummary = `E2E supervisor changed this work order while the technician was offline ${runId}`;
  watchPage(page, diagnostics);
  const supervisorContext = await browser.newContext({ baseURL, serviceWorkers: "allow" });
  const supervisorPage = await supervisorContext.newPage();
  watchPage(supervisorPage, diagnostics);

  try {
    await loginAs(supervisorPage, "supervisor@example.com");
    const assets = await getJson<{ assets: Json[] }>(supervisorContext, "/api/v1/assets/?q=TRK-012");
    const users = await getJson<{ users: Json[] }>(supervisorContext, "/api/v1/users/?role=technician");
    const assetId = String(assets.assets.find((asset) => asset.unit_number === "TRK-012")?.id ?? "");
    const technicianId = String(users.users.find((user) => user.username === "technician@example.com")?.id ?? "");
    expect(assetId).toBeTruthy();
    expect(technicianId).toBeTruthy();

    const created = await mutateJson<{ work_order: Json }>(
      supervisorContext,
      "POST",
      "/api/v1/maintenance/work-orders/",
      {
        asset_id: assetId,
        assigned_to_id: technicianId,
        summary: "E2E offline task conflict fixture",
        priority: "normal",
      },
    );
    const workOrderId = String(created.work_order.id);
    const taskResponse = await mutateJson<{ task: Json }>(
      supervisorContext,
      "POST",
      `/api/v1/maintenance/work-orders/${workOrderId}/tasks/`,
      { title: taskTitle, required: true, sequence: 1 },
    );
    const taskId = String(taskResponse.task.id);
    const ready = await mutateJson<{ work_order: Json }>(
      supervisorContext,
      "POST",
      `/api/v1/maintenance/work-orders/${workOrderId}/transition/`,
      { status: "Ready" },
    );

    await loginAs(page, "technician@example.com");
    await ensureServiceWorkerControls(page);
    await page.goto(`/work-orders/${workOrderId}`);
    await expect(page.getByRole("heading", { name: new RegExp(String(ready.work_order.number)) })).toBeVisible();
    await expect(page.getByText(taskTitle, { exact: true })).toBeVisible();

    await context.setOffline(true);
    await page.locator(".task-row").filter({
      has: page.getByText(taskTitle, { exact: true }),
    }).getByRole("button", { name: "Complete task", exact: true }).click();
    await expect(visibleSyncStatus(page).getByTestId("outbox-count")).toContainText("1 change pending");
    await expect.poll(async () => (await readStore<StoredOperation>(page, "outbox")).length).toBe(1);
    const [queued] = await readStore<StoredOperation>(page, "outbox");
    expect(queued).toMatchObject({
      type: "task.complete",
      status: "waiting",
      payload: {
        work_order_id: workOrderId,
        task_id: taskId,
        completed: true,
        base_version: ready.work_order.version,
      },
    });

    const changed = await mutateJson<{ work_order: Json }>(
      supervisorContext,
      "PATCH",
      `/api/v1/maintenance/work-orders/${workOrderId}/`,
      { summary: serverSummary, base_version: ready.work_order.version },
    );
    expect(Number(changed.work_order.version)).toBeGreaterThan(Number(ready.work_order.version));

    const syncResponsePromise = page.waitForResponse(
      (response) => response.url().endsWith("/api/v1/offline/sync/") && response.request().method() === "POST",
    );
    await context.setOffline(false);
    await visibleSyncStatus(page).click();
    const syncResponse = await syncResponsePromise;
    expect(syncResponse.status(), await syncResponse.text()).toBe(200);
    const syncResult = await syncResponse.json() as {
      results: { operation_id: string; status: string; message: string; server: Json }[];
    };
    expect(syncResult.results).toEqual([
      expect.objectContaining({
        operation_id: queued.operation_id,
        status: "conflict",
        message: "This work order changed on the server",
      }),
    ]);
    expect(syncResult.results[0].server).toMatchObject({
      work_order: {
        id: workOrderId,
        summary: serverSummary,
        version: changed.work_order.version,
      },
    });

    await expect(visibleSyncStatus(page)).toContainText("Sync conflict");
    await expect(visibleSyncStatus(page).getByTestId("outbox-count")).toHaveText("Needs your attention · 1 change");
    await expect(visibleSyncStatus(page)).toHaveAttribute("title", "This work order changed on the server");
    const [preserved] = await readStore<StoredOperation>(page, "outbox");
    expect(preserved).toMatchObject({
      operation_id: queued.operation_id,
      status: "conflict",
      message: "This work order changed on the server",
      payload: queued.payload,
    });

    const serverTask = await getJson<{ task: Json }>(
      context,
      `/api/v1/maintenance/work-orders/${workOrderId}/tasks/${taskId}/`,
    );
    expect(serverTask.task.status).toBe("Pending");
    const serverWork = await getJson<{ work_order: Json }>(
      context,
      `/api/v1/maintenance/work-orders/${workOrderId}/`,
    );
    expect(serverWork.work_order).toMatchObject({ summary: serverSummary, version: changed.work_order.version });

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Data quality" })).toBeVisible();
    await expect(page.getByText("This work order changed on the server", { exact: true })).toBeVisible();
    const conflict = page.locator(".list-row.danger").filter({
      has: page.getByText("Sync conflict · task.complete", { exact: true }),
    });
    await expect(conflict).toBeVisible();
    await conflict.getByText("Compare saved and server values", { exact: true }).click();
    await expect(conflict.getByRole("heading", { name: "Saved on this device" })).toBeVisible();
    await expect(conflict.locator("pre").nth(0)).toContainText('"completed": true');
    await expect(conflict.getByRole("heading", { name: "Current server value" })).toBeVisible();
    await expect(conflict.locator("pre").nth(1)).toContainText(serverSummary);
    const reviewCurrentWork = conflict.getByRole("link", { name: "Review current work" });
    await expect(reviewCurrentWork).toHaveAttribute("href", `/work-orders/${workOrderId}`);
    await expect(conflict.getByRole("button", { name: "Discard saved change" })).toBeVisible();
    await reviewCurrentWork.click();
    await expect(page).toHaveURL(new RegExp(`/work-orders/${workOrderId}$`));
    await expect(page.getByRole("paragraph").filter({ hasText: serverSummary })).toBeVisible();
    expect(await readStore<StoredOperation>(page, "outbox")).toMatchObject([
      { operation_id: queued.operation_id, status: "conflict", payload: queued.payload },
    ]);

    await testInfo.attach("offline-conflict-state", {
      body: Buffer.from(JSON.stringify({ queued, changed: changed.work_order, syncResult, preserved, serverTask }, null, 2)),
      contentType: "application/json",
    });
    assertOnlyExpectedOfflineFailures(diagnostics);
  } finally {
    await context.setOffline(false).catch(() => undefined);
    await supervisorContext.close();
    await attachDiagnostics(testInfo, diagnostics);
  }
});

test("@offline a missing saved attachment becomes a durable conflict without acknowledging the operation", async ({ page, context }) => {
  await loginAs(page, "driver@example.com");
  const me = await getJson<{ user: { id: string; organization_id: string } }>(context, "/api/v1/auth/me/");
  const identity = await storedOfflineIdentity(page);
  const operationId = randomUUID();
  const missingBlobId = randomUUID();
  await putStore<StoredOperation>(page, "outbox", [{
    operation_id: operationId,
    type: "defect.create",
    payload: { description: "Missing attachment must block sync" },
    created_at: new Date().toISOString(),
    status: "waiting",
    user_id: me.user.id,
    organization_id: me.user.organization_id,
    expires_at: identity.expires_at,
    offline_grant: identity.offline_grant,
    blob_ids: [missingBlobId],
  }]);
  await deleteStoreRecord(page, "blobs", missingBlobId);

  let syncRequests = 0;
  await page.route("**/api/v1/offline/sync/", async (route) => {
    syncRequests += 1;
    await route.continue();
  });
  await page.reload({ waitUntil: "domcontentloaded" });

  await expect(visibleSyncStatus(page)).toContainText("Sync conflict");
  await expect.poll(async () => (await readStore<StoredOperation>(page, "outbox"))[0]?.status).toBe("conflict");
  const [preserved] = await readStore<StoredOperation>(page, "outbox");
  expect(preserved).toMatchObject({
    operation_id: operationId,
    status: "conflict",
    message: "A saved attachment is missing from this device. The change was not synchronized.",
    blob_ids: [missingBlobId],
  });
  expect(syncRequests).toBe(0);
});

test("@offline sync submits at most 100 operations per deterministic batch and continues the remainder", async ({ page, context }) => {
  test.setTimeout(90_000);
  await loginAs(page, "driver@example.com");
  const me = await getJson<{ user: { id: string; organization_id: string } }>(context, "/api/v1/auth/me/");
  const bootstrap = await getJson<{ assets: { id: string }[] }>(context, "/api/v1/bootstrap/");
  const identity = await storedOfflineIdentity(page);
  const assetId = bootstrap.assets[0]?.id;
  expect(assetId).toBeTruthy();
  const operations = Array.from({ length: 101 }, (_, index): StoredOperation => ({
    operation_id: randomUUID(),
    type: "defect.create",
    payload: {
      asset_id: assetId,
      category: "Other",
      severity: "medium",
      safety_related: false,
      description: `Deterministic batch operation ${String(index).padStart(3, "0")}`,
    },
    created_at: new Date(Date.now() + index).toISOString(),
    status: "waiting",
    user_id: me.user.id,
    organization_id: me.user.organization_id,
    expires_at: identity.expires_at,
    offline_grant: identity.offline_grant,
    blob_ids: [],
  }));
  const batches: string[][] = [];
  await page.route("**/api/v1/offline/sync/", async (route) => {
    const request = route.request().postDataJSON() as { operations: { operation_id: string }[] };
    const ids = request.operations.map((operation) => operation.operation_id);
    batches.push(ids);
    await route.continue();
  });
  await putStore(page, "outbox", operations);
  await page.reload({ waitUntil: "domcontentloaded" });

  await expect.poll(async () => (await readStore<StoredOperation>(page, "outbox")).length).toBe(0);
  expect(batches.map((batch) => batch.length)).toEqual([100, 1]);
  expect(batches.flat()).toEqual(operations.map((operation) => operation.operation_id));
  await expect(visibleSyncStatus(page)).toContainText("Synchronized");
  const defects = await getJson<{ defects: Json[] }>(context, "/api/v1/maintenance/defects/");
  expect(defects.defects.filter((row) => String(row.description ?? "").startsWith("Deterministic batch operation "))).toHaveLength(101);
});

test("@offline an account switch clears readable cache, preserves prior owned work, and refuses cross-account sync", async ({ page, context }) => {
  await loginAs(page, "driver@example.com");
  const driver = await getJson<{ user: { id: string; organization_id: string } }>(context, "/api/v1/auth/me/");
  const identity = await storedOfflineIdentity(page);
  const operation: StoredOperation = {
    operation_id: randomUUID(),
    type: "defect.create",
    payload: { description: "Driver-owned work must survive a local account switch" },
    created_at: new Date().toISOString(),
    status: "waiting",
    user_id: driver.user.id,
    organization_id: driver.user.organization_id,
    expires_at: identity.expires_at,
    offline_grant: identity.offline_grant,
    blob_ids: [],
  };
  await putStore(page, "outbox", [operation]);
  await putStore(page, "cache", [{
    key: "work-order:driver-sensitive-fixture",
    value: { data: { secret: "old principal cache" } },
    cached_at: new Date().toISOString(),
    user_id: driver.user.id,
    organization_id: driver.user.organization_id,
    expires_at: identity.expires_at,
    offline_grant: identity.offline_grant,
  }]);

  let crossAccountSyncs = 0;
  await page.route("**/api/v1/offline/sync/", async (route) => {
    crossAccountSyncs += 1;
    await route.continue();
  });
  await mutateJson(context, "POST", "/api/v1/auth/login/", {
    username: "technician@example.com",
    password,
  });
  const technician = await getJson<{ user: { id: string; organization_id: string } }>(context, "/api/v1/auth/me/");
  expect(technician.user.id).not.toBe(driver.user.id);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "My work" })).toBeVisible();
  await expect(visibleSyncStatus(page).getByTestId("outbox-count")).toContainText("All changes are up to date");

  expect(crossAccountSyncs).toBe(0);
  expect(await readStore<StoredOperation>(page, "outbox")).toMatchObject([operation]);
  const cacheRows = await readStore<{ key: string; user_id: string }>(page, "cache");
  expect(cacheRows.some((row) => row.key === "work-order:driver-sensitive-fixture")).toBe(false);
  expect(cacheRows.every((row) => row.user_id === technician.user.id)).toBe(true);
});

test("@offline the service worker never stores API responses", async ({ page }) => {
  await loginAs(page, "driver@example.com");
  await ensureServiceWorkerControls(page);
  await page.evaluate(async () => {
    const response = await fetch("/api/v1/bootstrap/", { credentials: "same-origin" });
    if (!response.ok) throw new Error(`bootstrap failed (${response.status})`);
  });
  const cachedUrls = await page.evaluate(async () => {
    const urls: string[] = [];
    for (const name of await caches.keys()) {
      const cache = await caches.open(name);
      urls.push(...(await cache.keys()).map((request) => request.url));
    }
    return urls;
  });
  expect(cachedUrls.filter((url) => new URL(url).pathname.startsWith("/api/"))).toEqual([]);
});
