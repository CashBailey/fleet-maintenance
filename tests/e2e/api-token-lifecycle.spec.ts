import { createHmac, randomUUID } from "node:crypto";

import { expect, type Page, type Response as PageResponse, test } from "@playwright/test";

import { collectBrowserDiagnostics } from "./helpers";

const baseURL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:8088";
const password = process.env.E2E_PASSWORD ?? "DemoPass123!";
const mfaKey = Buffer.from("48656c6c6f21deadbeef", "hex");

interface TokenSummary {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  user_id: string;
  user: string;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
}

interface TokenResponse {
  api_token: TokenSummary;
  token?: string;
  secret_recoverable: boolean;
  message?: string;
}

interface AuditEvent {
  action: string;
  actor: string | null;
  correlation_id: string;
  context: Record<string, unknown>;
  new_state: string;
  previous_state: string;
  resource_id: string;
  resource_type: string;
}

interface JsonReadable {
  json(): Promise<unknown>;
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

async function login(page: Page, username: string, mfa = false): Promise<void> {
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
  await expect(page.locator("main")).toBeVisible();
}

async function csrf(page: Page): Promise<string> {
  const response = await page.context().request.get("/api/v1/auth/csrf/");
  expect(response.status(), await response.text()).toBe(200);
  return String((await response.json()).csrf_token);
}

function localDateTime(date: Date): string {
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

async function json<T>(response: JsonReadable): Promise<T> {
  return await response.json() as T;
}

async function persistedSecretLocations(page: Page, secret: string): Promise<string[]> {
  return page.evaluate(async (needle) => {
    const matches: string[] = [];
    const check = (location: string, value: unknown) => {
      const serialized = JSON.stringify(value);
      if (serialized?.includes(needle)) matches.push(location);
    };

    check("localStorage", { ...localStorage });
    check("sessionStorage", { ...sessionStorage });

    for (const cacheName of await caches.keys()) {
      const cache = await caches.open(cacheName);
      for (const request of await cache.keys()) {
        const response = await cache.match(request);
        check(`cache:${cacheName}:${request.url}`, await response?.clone().text());
      }
    }

    for (const database of await indexedDB.databases()) {
      if (!database.name) continue;
      const contents = await new Promise<Record<string, unknown[]>>((resolve) => {
        const opening = indexedDB.open(database.name!);
        opening.onerror = () => resolve({});
        opening.onsuccess = () => {
          const db = opening.result;
          const names = Array.from(db.objectStoreNames);
          if (!names.length) {
            db.close();
            resolve({});
            return;
          }
          const values: Record<string, unknown[]> = {};
          const transaction = db.transaction(names, "readonly");
          for (const name of names) {
            const request = transaction.objectStore(name).getAll();
            request.onsuccess = () => { values[name] = request.result as unknown[]; };
          }
          transaction.oncomplete = () => { db.close(); resolve(values); };
          transaction.onerror = () => { db.close(); resolve({}); };
          transaction.onabort = () => { db.close(); resolve({}); };
        };
      });
      check(`indexedDB:${database.name}`, contents);
    }
    return matches;
  }, secret);
}

test("API token lifecycle is one-time, scoped, tenant-bound, auditable, and revocable", async ({ browser, page, request }, testInfo) => {
  test.setTimeout(120_000);
  const otherContext = await browser.newContext({ baseURL });
  const otherPage = await otherContext.newPage();
  let diagnostics: ReturnType<typeof collectBrowserDiagnostics> | undefined;
  let otherDiagnostics: ReturnType<typeof collectBrowserDiagnostics> | undefined;
  try {
    await login(page, "system.admin@example.com", true);
    diagnostics = collectBrowserDiagnostics(page);
    await expect(page.getByRole("heading", { level: 1, name: "System", exact: true })).toBeVisible();

    const usersResponse = await page.context().request.get("/api/v1/users/?role=supervisor");
    expect(usersResponse.status(), await usersResponse.text()).toBe(200);
    const supervisors = (await json<{ users: Array<{ id: string; username: string }> }>(usersResponse)).users;
    const supervisor = supervisors.find(({ username }) => username === "supervisor@example.com");
    expect(supervisor, "deterministic supervisor seed user").toBeDefined();

    const supervisorContext = await browser.newContext({ baseURL });
    const supervisorPage = await supervisorContext.newPage();
    await login(supervisorPage, "supervisor@example.com");
    const bootstrapResponse = await supervisorContext.request.get("/api/v1/bootstrap/");
    expect(bootstrapResponse.status(), await bootstrapResponse.text()).toBe(200);
    const bootstrap = await json<{
      user: { organization_id: string };
      work_orders: Array<{ id: string }>;
    }>(bootstrapResponse);
    expect(bootstrap.work_orders.length, "seed work order for delegated-scope checks").toBeGreaterThan(0);
    const workOrderId = bootstrap.work_orders[0].id;
    await supervisorContext.close();

    const noteAuditsBeforeResponse = await page.context().request.get(
      `/api/v1/audit-events/?resource_type=WorkOrder&resource_id=${workOrderId}`,
    );
    expect(noteAuditsBeforeResponse.status(), await noteAuditsBeforeResponse.text()).toBe(200);
    const workOrderAuditsBefore = (await json<{ events: AuditEvent[] }>(noteAuditsBeforeResponse)).events;
    const noteAuditsBefore = workOrderAuditsBefore.filter(
      ({ action }) => action === "work_order.note_created",
    ).length;
    const transitionAuditsBefore = workOrderAuditsBefore.filter(
      ({ action }) => action === "work_order.transitioned",
    ).length;

    await page.goto("/administration");
    await expect(page.getByRole("heading", { level: 1, name: "Administration", exact: true })).toBeVisible();
    await page.getByLabel(/^Token user/).selectOption(supervisor!.id);
    const tokenName = `E2E shop reporting ${randomUUID()}`;
    await page.getByLabel(/^Token name/).fill(tokenName);
    await page.getByLabel(/^Scopes/).fill("reports.shop");
    await page.getByLabel(/^Expires at/).fill(localDateTime(new Date(Date.now() + 24 * 60 * 60 * 1_000)));

    const [createRequest, createResponse] = await Promise.all([
      page.waitForRequest(
        (candidate) => candidate.url().endsWith("/api/v1/api-tokens/") && candidate.method() === "POST",
      ),
      page.waitForResponse(
        (candidate) => candidate.url().endsWith("/api/v1/api-tokens/") && candidate.request().method() === "POST",
      ),
      page.getByRole("button", { name: "Create API token", exact: true }).click(),
    ]);
    expect(createResponse.status(), await createResponse.text()).toBe(201);
    const created = await json<TokenResponse>(createResponse);
    const rawToken = created.token ?? "";
    expect(created.secret_recoverable).toBe(true);
    expect(rawToken).toMatch(new RegExp(`^flt_${created.api_token.prefix}_[A-Za-z0-9_-]+$`));
    expect(created.api_token).toMatchObject({
      name: tokenName,
      scopes: ["reports.shop"],
      user_id: supervisor!.id,
      last_used_at: null,
      revoked_at: null,
    });
    expect(new Date(created.api_token.expires_at).valueOf()).toBeGreaterThan(Date.now());
    const oneTimeNotice = page.getByRole("status").filter({ hasText: "Copy this API token now" });
    await expect(oneTimeNotice.getByRole("heading", { name: "Copy this API token now", exact: true })).toBeVisible();
    await expect(oneTimeNotice.locator("code")).toHaveText(rawToken);
    await expect(oneTimeNotice).toContainText("This secret will not be shown again.");

    const issueKey = createRequest.headers()["idempotency-key"];
    expect(issueKey).toMatch(/^[0-9a-f-]{36}$/);
    const issuePayload = createRequest.postDataJSON() as {
      user_id: string;
      name: string;
      scopes: string[];
      expires_at: string;
    };
    const replayHeaders = { "X-CSRFToken": await csrf(page), "Idempotency-Key": issueKey };
    const replayResponses = await Promise.all([
      page.context().request.post("/api/v1/api-tokens/", { headers: replayHeaders, data: issuePayload }),
      page.context().request.post("/api/v1/api-tokens/", { headers: replayHeaders, data: issuePayload }),
    ]);
    for (const replayResponse of replayResponses) {
      expect(replayResponse.status(), await replayResponse.text()).toBe(201);
      const replay = await json<TokenResponse>(replayResponse);
      expect(replay.api_token.id).toBe(created.api_token.id);
      expect(replay.secret_recoverable).toBe(false);
      expect(replay).not.toHaveProperty("token");
      expect(replay.message).toContain("cannot be recovered");
      expect(JSON.stringify(replay)).not.toContain(rawToken);
    }

    const listResponse = await page.context().request.get("/api/v1/api-tokens/");
    expect(listResponse.status(), await listResponse.text()).toBe(200);
    const tokenList = (await json<{ api_tokens: TokenSummary[] }>(listResponse)).api_tokens;
    const matchingTokens = tokenList.filter(({ name }) => name === tokenName);
    expect(matchingTokens).toHaveLength(1);
    expect(matchingTokens[0].id).toBe(created.api_token.id);
    expect(matchingTokens[0]).not.toHaveProperty("token");
    expect(matchingTokens[0]).not.toHaveProperty("token_hash");
    expect(JSON.stringify(tokenList)).not.toContain(rawToken);
    expect(await persistedSecretLocations(page, rawToken)).toEqual([]);

    const issuanceAuditResponse = await page.context().request.get(
      `/api/v1/audit-events/?resource_type=ApiToken&resource_id=${created.api_token.id}`,
    );
    expect(issuanceAuditResponse.status(), await issuanceAuditResponse.text()).toBe(200);
    const issuanceAudits = (await json<{ events: AuditEvent[] }>(issuanceAuditResponse)).events;
    expect(issuanceAudits.filter(({ action }) => action === "api_token.issued")).toEqual([
      expect.objectContaining({
        actor: "system.admin@example.com",
        correlation_id: issueKey,
        new_state: "active",
      }),
    ]);
    expect(JSON.stringify(issuanceAudits)).not.toContain(rawToken);

    const bearerHeaders = { Authorization: `Bearer ${rawToken}` };
    const reportResponse = await request.get("/api/v1/reports/operations/", { headers: bearerHeaders });
    expect(reportResponse.status(), await reportResponse.text()).toBe(200);
    expect(await json<Record<string, unknown>>(reportResponse)).toHaveProperty("summary");

    const directDenial = await request.post(`/api/v1/maintenance/work-orders/${workOrderId}/transition/`, {
      headers: { ...bearerHeaders, "Idempotency-Key": randomUUID() },
      data: { status: "Ready", reason: "Must remain blocked by delegated scope" },
    });
    expect(directDenial.status(), await directDenial.text()).toBe(403);
    expect(await json<Record<string, unknown>>(directDenial)).toMatchObject({
      error: { code: "permission_denied" },
    });

    const offlineOperationId = randomUUID();
    const offlineDenial = await request.post("/api/v1/offline/sync/", {
      headers: bearerHeaders,
      data: {
        operations: [{
          operation_id: offlineOperationId,
          type: "work_note.create",
          payload: { work_order_id: workOrderId, body: `Forbidden token note ${offlineOperationId}` },
        }],
      },
    });
    expect(offlineDenial.status(), await offlineDenial.text()).toBe(403);
    expect(await json<Record<string, unknown>>(offlineDenial)).toMatchObject({
      error: { code: "invalid_offline_grant" },
    });

    const noteAuditsAfterResponse = await page.context().request.get(
      `/api/v1/audit-events/?resource_type=WorkOrder&resource_id=${workOrderId}`,
    );
    expect(noteAuditsAfterResponse.status(), await noteAuditsAfterResponse.text()).toBe(200);
    const workOrderAuditsAfter = (await json<{ events: AuditEvent[] }>(noteAuditsAfterResponse)).events;
    const noteAuditsAfter = workOrderAuditsAfter.filter(
      ({ action }) => action === "work_order.note_created",
    ).length;
    const transitionAuditsAfter = workOrderAuditsAfter.filter(
      ({ action }) => action === "work_order.transitioned",
    ).length;
    expect(noteAuditsAfter, "a rejected offline operation has no durable side effect").toBe(noteAuditsBefore);
    expect(transitionAuditsAfter, "a forbidden direct transition has no durable side effect").toBe(
      transitionAuditsBefore,
    );

    const bearerManagementDenial = await request.get("/api/v1/api-tokens/", { headers: bearerHeaders });
    expect(bearerManagementDenial.status(), await bearerManagementDenial.text()).toBe(403);

    const refreshedListResponse = await page.context().request.get("/api/v1/api-tokens/");
    expect(refreshedListResponse.status(), await refreshedListResponse.text()).toBe(200);
    const refreshedToken = (await json<{ api_tokens: TokenSummary[] }>(refreshedListResponse)).api_tokens
      .find(({ id }) => id === created.api_token.id);
    expect(refreshedToken?.last_used_at).not.toBeNull();
    expect(new Date(refreshedToken!.last_used_at!).valueOf()).toBeGreaterThanOrEqual(
      new Date(created.api_token.created_at).valueOf(),
    );

    const refreshPromise = page.waitForResponse(
      (candidate) => candidate.url().endsWith("/api/v1/api-tokens/") && candidate.request().method() === "GET",
    );
    await page.reload({ waitUntil: "domcontentloaded" });
    expect((await refreshPromise).status()).toBe(200);
    await expect(page.getByRole("heading", { level: 1, name: "Administration", exact: true })).toBeVisible();
    await expect(page.getByText(rawToken, { exact: true })).toHaveCount(0);
    expect(await persistedSecretLocations(page, rawToken)).toEqual([]);
    const tokenPanel = page.locator("section.panel").filter({
      has: page.getByRole("heading", { level: 2, name: "API tokens", exact: true }),
    });
    const tokenRow = tokenPanel.locator(".workflow-row").filter({ hasText: tokenName });
    await expect(tokenRow).toContainText("Active");
    await expect(tokenRow).toContainText("reports.shop");
    await expect(tokenRow).not.toContainText("last used —");

    await login(otherPage, "other.manager@example.com");
    otherDiagnostics = collectBrowserDiagnostics(otherPage);
    await expect(otherPage.getByRole("link", { name: "Administration", exact: true })).toHaveCount(0);
    const otherManagement = await otherContext.request.get("/api/v1/api-tokens/");
    expect(otherManagement.status(), await otherManagement.text()).toBe(403);
    const otherMeResponse = await otherContext.request.get("/api/v1/auth/me/");
    expect(otherMeResponse.status(), await otherMeResponse.text()).toBe(200);
    const otherUser = (await json<{ user: { id: string; organization_id: string } }>(otherMeResponse)).user;
    expect(otherUser.organization_id).not.toBe(bootstrap.user.organization_id);
    const crossTenantIssue = await page.context().request.post("/api/v1/api-tokens/", {
      headers: { "X-CSRFToken": replayHeaders["X-CSRFToken"], "Idempotency-Key": randomUUID() },
      data: { ...issuePayload, user_id: otherUser.id, name: `${tokenName} cross-tenant` },
    });
    expect(crossTenantIssue.status(), await crossTenantIssue.text()).toBe(404);
    expect(await json<Record<string, unknown>>(crossTenantIssue)).toMatchObject({
      error: { code: "not_found" },
    });

    const revokeReason = "E2E delegated reporting integration retired";
    let revokePromptSeen = false;
    page.once("dialog", (dialog) => {
      revokePromptSeen = true;
      expect(dialog.type()).toBe("prompt");
      expect(dialog.message()).toContain(tokenName);
      void dialog.accept(revokeReason);
    });
    const [revokeRequest, revokeResponse] = await Promise.all([
      page.waitForRequest(
        (candidate) => candidate.url().endsWith(`/api/v1/api-tokens/${created.api_token.id}/revoke/`) && candidate.method() === "POST",
      ),
      page.waitForResponse(
        (candidate) => candidate.url().endsWith(`/api/v1/api-tokens/${created.api_token.id}/revoke/`) && candidate.request().method() === "POST",
      ),
      tokenRow.getByRole("button", { name: "Revoke API token", exact: true }).click(),
    ]);
    expect(revokePromptSeen).toBe(true);
    expect(revokeRequest.postDataJSON()).toEqual({ reason: revokeReason });
    expect(revokeResponse.status(), await revokeResponse.text()).toBe(200);
    const revoked = await json<{ api_token: TokenSummary }>(revokeResponse);
    expect(revoked.api_token.revoked_at).not.toBeNull();
    await expect(page.getByRole("status").filter({ hasText: "API token revoked." })).toBeVisible();
    await expect(tokenRow).toContainText("Revoked");
    await expect(tokenRow.getByRole("button", { name: "Revoke API token", exact: true })).toHaveCount(0);

    const revokedBearer = await request.get("/api/v1/reports/operations/", { headers: bearerHeaders });
    expect(revokedBearer.status(), await revokedBearer.text()).toBe(403);

    const auditResponse = await page.context().request.get(
      `/api/v1/audit-events/?resource_type=ApiToken&resource_id=${created.api_token.id}`,
    );
    expect(auditResponse.status(), await auditResponse.text()).toBe(200);
    const audits = (await json<{ events: AuditEvent[] }>(auditResponse)).events;
    expect(audits.filter(({ action }) => action === "api_token.issued")).toHaveLength(1);
    expect(audits.filter(({ action }) => action === "api_token.revoked")).toEqual([
      expect.objectContaining({
        actor: "system.admin@example.com",
        correlation_id: revokeRequest.headers()["idempotency-key"],
        context: expect.objectContaining({ reason: revokeReason }),
        previous_state: "active",
        new_state: "revoked",
      }),
    ]);
    expect(JSON.stringify(audits)).not.toContain(rawToken);

    diagnostics.assertClean();
    otherDiagnostics.assertClean();
  } finally {
    if (diagnostics) await diagnostics.attach(testInfo);
    if (otherDiagnostics) await otherDiagnostics.attach(testInfo);
    await otherContext.close();
  }
});
