import { expect, test, type Browser, type BrowserContext, type Page, type Response } from "@playwright/test";

import { collectBrowserDiagnostics, e2eUser } from "./helpers";

type Json = Record<string, unknown>;
type ApiResult = { status: number; body: Json };

const issueUrl = "**/api/v1/inventory/issues/";
const baseURL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:8088";

function operationKey(value: number): string {
  return `00000000-0000-4000-8000-${String(value).padStart(12, "0")}`;
}

function record(value: unknown, label: string): Json {
  expect(value, `${label} must be an object`).not.toBeNull();
  expect(typeof value, `${label} must be an object`).toBe("object");
  expect(Array.isArray(value), `${label} must be an object`).toBe(false);
  return value as Json;
}

function rows(value: unknown, label: string): Json[] {
  expect(Array.isArray(value), `${label} must be an array`).toBe(true);
  return value as Json[];
}

function id(value: unknown, label: string): string {
  const result = String(record(value, label).id ?? "");
  expect(result, `${label}.id`).toMatch(/^[0-9a-f-]{36}$/i);
  return result;
}

async function loginAs(page: Page, username: string): Promise<void> {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.getByLabel(/username|email/i).first().fill(username);
  await page.getByLabel(/password/i).first().fill(e2eUser.password);
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/login/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: /sign in|log in/i }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBeLessThan(300);
  await expect(page.getByRole("heading", { name: /parts|today|purchase orders/i }).first()).toBeVisible();
  await expect(page.locator("main")).toBeVisible();
}

async function getJson(page: Page, path: string): Promise<ApiResult> {
  return page.evaluate(async (requestPath) => {
    const response = await fetch(requestPath, { credentials: "same-origin", headers: { Accept: "application/json" } });
    return { status: response.status, body: await response.json() as Json };
  }, path);
}

async function postJson(page: Page, path: string, body: Json, key: string): Promise<ApiResult> {
  return page.evaluate(async ({ requestPath, requestBody, operationId }) => {
    const csrfResponse = await fetch("/api/v1/auth/csrf/", { credentials: "same-origin" });
    const csrf = await csrfResponse.json() as { csrf_token?: string };
    const response = await fetch(requestPath, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": operationId,
        "X-CSRFToken": csrf.csrf_token ?? "",
      },
      body: JSON.stringify(requestBody),
    });
    return { status: response.status, body: await response.json() as Json };
  }, { requestPath: path, requestBody: body, operationId: key });
}

interface Scenario {
  partsContext: BrowserContext;
  supervisorContext: BrowserContext;
  partsPage: Page;
  supervisorPage: Page;
  partId: string;
  partNumber: string;
  binId: string;
  workOrderId: string;
}

async function setupScenario(
  browser: Browser,
  options: { partNumber: string; name: string; quantity: number; keyBase: number },
): Promise<Scenario> {
  const [partsContext, supervisorContext] = await Promise.all([
    browser.newContext({ baseURL }),
    browser.newContext({ baseURL }),
  ]);
  const managerContext = await browser.newContext({ baseURL });
  const partsPage = await partsContext.newPage();
  const supervisorPage = await supervisorContext.newPage();
  const managerPage = await managerContext.newPage();

  try {
    await Promise.all([
      loginAs(partsPage, "parts.clerk@example.com"),
      loginAs(supervisorPage, "supervisor@example.com"),
      loginAs(managerPage, "purchasing.manager@example.com"),
    ]);

    const [binsResult, bootstrapResult] = await Promise.all([
      getJson(partsPage, "/api/v1/inventory/bins/"),
      getJson(supervisorPage, "/api/v1/bootstrap/"),
    ]);
    expect(binsResult.status, JSON.stringify(binsResult.body)).toBe(200);
    expect(bootstrapResult.status, JSON.stringify(bootstrapResult.body)).toBe(200);

    const bin = rows(binsResult.body.bins, "bins").find((item) => item.code === "A-01");
    const asset = rows(bootstrapResult.body.assets, "bootstrap assets")[0];
    expect(bin, "seed bin MAIN/A-01").toBeTruthy();
    expect(asset, "seed asset").toBeTruthy();

    const partResult = await postJson(partsPage, "/api/v1/inventory/parts/", {
      number: options.partNumber,
      name: options.name,
      unit_of_measure: "each",
    }, operationKey(options.keyBase));
    expect(partResult.status, JSON.stringify(partResult.body)).toBe(201);
    const partId = id(partResult.body.part, "created part");
    const binId = id(bin, "seed bin");

    const workResult = await postJson(supervisorPage, "/api/v1/maintenance/work-orders/", {
      asset_id: id(asset, "seed asset"),
      summary: `${options.name} contention work`,
      priority: "normal",
    }, operationKey(options.keyBase + 1));
    expect(workResult.status, JSON.stringify(workResult.body)).toBe(201);
    const workOrderId = id(workResult.body.work_order, "created work order");

    const adjustmentResult = await postJson(managerPage, "/api/v1/inventory/adjustments/", {
      part_id: partId,
      bin_id: binId,
      quantity: String(options.quantity),
      reason: `${options.name} auditable E2E opening balance`,
    }, operationKey(options.keyBase + 2));
    expect(adjustmentResult.status, JSON.stringify(adjustmentResult.body)).toBe(201);

    await Promise.all([partsPage.reload(), supervisorPage.reload()]);
    await managerContext.close();
    return {
      partsContext,
      supervisorContext,
      partsPage,
      supervisorPage,
      partId,
      partNumber: options.partNumber,
      binId,
      workOrderId,
    };
  } catch (error) {
    await Promise.all([partsContext.close(), supervisorContext.close(), managerContext.close()]);
    throw error;
  }
}

async function prepareIssue(page: Page, scenario: Scenario): Promise<void> {
  await page.goto("/inventory");
  await page.getByRole("tab", { name: "Issue part" }).click();
  await page.getByRole("combobox", { name: "Part", exact: true }).selectOption(scenario.partId);
  await page.getByRole("combobox", { name: "Bin", exact: true }).selectOption(scenario.binId);
  await page.getByLabel("Work order ID").fill(scenario.workOrderId);
  await page.getByLabel(/^Quantity\b/).fill("1");
}

async function assertSingleIssue(page: Page, scenario: Scenario, expectedOnHand = "0.000"): Promise<Json> {
  const [historyResult, stockResult] = await Promise.all([
    getJson(page, `/api/v1/inventory/parts/${scenario.partId}/history/`),
    getJson(page, `/api/v1/inventory/stock/?part_id=${scenario.partId}`),
  ]);
  expect(historyResult.status, JSON.stringify(historyResult.body)).toBe(200);
  expect(stockResult.status, JSON.stringify(stockResult.body)).toBe(200);
  const issues = rows(historyResult.body.transactions, "part transactions").filter(
    (transaction) => transaction.type === "ISSUE",
  );
  expect(issues, `issue ledger rows for ${scenario.partNumber}`).toHaveLength(1);
  expect(issues[0].quantity).toBe("-1.000");

  const balance = rows(stockResult.body.stock, "stock balances").find(
    (item) => item.part_id === scenario.partId && item.bin_id === scenario.binId,
  );
  expect(balance, `stock balance for ${scenario.partNumber}`).toMatchObject({
    quantity_on_hand: expectedOnHand,
    quantity_reserved: "0.000",
    available_quantity: expectedOnHand,
  });
  return issues[0];
}

function collectExpectedIssueAbort(page: Page) {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const failedRequests: { method: string; url: string; error: string }[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("requestfailed", (request) => {
    failedRequests.push({
      method: request.method(),
      url: request.url(),
      error: request.failure()?.errorText ?? "unknown",
    });
  });
  return {
    assertOnlyExpectedFailure() {
      expect(pageErrors, "no page errors during the interrupted request").toEqual([]);
      expect(
        consoleErrors.filter((message) => !/Failed to load resource.*ERR_(?:CONNECTION_RESET|FAILED)/i.test(message)),
        "no unexpected console errors during the interrupted request",
      ).toEqual([]);
      expect(failedRequests, "the committed response is hidden by exactly one simulated connection reset").toEqual([
        expect.objectContaining({
          method: "POST",
          url: expect.stringMatching(/\/api\/v1\/inventory\/issues\/$/),
          error: expect.stringMatching(/ERR_(?:CONNECTION_RESET|FAILED)/i),
        }),
      ]);
    },
  };
}

test("two users cannot issue the final available unit", async ({ browser }, testInfo) => {
  const scenario = await setupScenario(browser, {
    partNumber: "E2E-CONCURRENT-1",
    name: "Concurrent final unit",
    quantity: 1,
    keyBase: 100,
  });
  const partsDiagnostics = collectBrowserDiagnostics(scenario.partsPage);
  const supervisorDiagnostics = collectBrowserDiagnostics(scenario.supervisorPage);

  try {
    await Promise.all([
      prepareIssue(scenario.partsPage, scenario),
      prepareIssue(scenario.supervisorPage, scenario),
    ]);
    const partsResponse = scenario.partsPage.waitForResponse(
      (response) => response.url().endsWith("/api/v1/inventory/issues/") && response.request().method() === "POST",
    );
    const supervisorResponse = scenario.supervisorPage.waitForResponse(
      (response) => response.url().endsWith("/api/v1/inventory/issues/") && response.request().method() === "POST",
    );
    await Promise.all([
      scenario.partsPage.getByRole("button", { name: "Issue part" }).click(),
      scenario.supervisorPage.getByRole("button", { name: "Issue part" }).click(),
    ]);
    const responses = await Promise.all([partsResponse, supervisorResponse]);
    const statuses = responses.map((response) => response.status()).sort();
    expect(statuses).toEqual([201, 409]);

    const winner = responses[0].status() === 201 ? scenario.partsPage : scenario.supervisorPage;
    const loser = responses[0].status() === 409 ? scenario.partsPage : scenario.supervisorPage;
    await expect(winner.getByText("Issue transaction recorded.", { exact: true })).toBeVisible();
    await expect(loser.getByRole("alert")).toContainText("Insufficient available stock");

    const issue = await assertSingleIssue(scenario.partsPage, scenario);
    const auditResult = await getJson(
      scenario.supervisorPage,
      `/api/v1/audit-events/?resource_type=StockTransaction&resource_id=${String(issue.id)}`,
    );
    expect(auditResult.status, JSON.stringify(auditResult.body)).toBe(200);
    expect(rows(auditResult.body.events, "stock audit events").filter((event) => event.action === "stock.issued"))
      .toHaveLength(1);
    partsDiagnostics.assertClean();
    supervisorDiagnostics.assertClean();
  } finally {
    await partsDiagnostics.attach(testInfo);
    await supervisorDiagnostics.attach(testInfo);
    await Promise.all([scenario.partsContext.close(), scenario.supervisorContext.close()]);
  }
});

test("double-clicking an issue records one ledger transaction and a clear result", async ({ browser }, testInfo) => {
  const scenario = await setupScenario(browser, {
    partNumber: "E2E-DOUBLE-CLICK-1",
    name: "Double click issue",
    quantity: 2,
    keyBase: 200,
  });
  const diagnostics = collectBrowserDiagnostics(scenario.partsPage);

  try {
    await prepareIssue(scenario.partsPage, scenario);
    const responses: Response[] = [];
    const collectResponse = (response: Response) => {
      if (response.url().endsWith("/api/v1/inventory/issues/") && response.request().method() === "POST") {
        responses.push(response);
      }
    };
    scenario.partsPage.on("response", collectResponse);
    await scenario.partsPage.getByRole("button", { name: "Issue part" }).dblclick();
    await expect.poll(() => responses.length, "the double-click produced one in-flight mutation").toBe(1);
    scenario.partsPage.off("response", collectResponse);

    expect(responses[0].status()).toBe(201);
    expect((await responses[0].request().allHeaders())["idempotency-key"]).toMatch(/^[0-9a-f-]{36}$/i);
    await expect(scenario.partsPage.getByText("Issue transaction recorded.", { exact: true })).toBeVisible();
    await expect(scenario.partsPage.getByRole("alert")).toHaveCount(0);
    await expect(scenario.partsPage.getByRole("button", { name: "Issue part" })).toBeEnabled();
    await assertSingleIssue(scenario.partsPage, scenario, "1.000");
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
    await Promise.all([scenario.partsContext.close(), scenario.supervisorContext.close()]);
  }
});

test("an apparent network failure retries the same issue idempotently", async ({ browser }, testInfo) => {
  const scenario = await setupScenario(browser, {
    partNumber: "E2E-RETRY-1",
    name: "Interrupted issue retry",
    quantity: 2,
    keyBase: 300,
  });
  const diagnostics = collectBrowserDiagnostics(scenario.partsPage);
  const expectedAbort = collectExpectedIssueAbort(scenario.partsPage);

  try {
    await prepareIssue(scenario.partsPage, scenario);
    let resolveFirst!: (result: { status: number; body: Json; key: string }) => void;
    let rejectFirst!: (reason: unknown) => void;
    const firstAttempt = new Promise<{ status: number; body: Json; key: string }>((resolve, reject) => {
      resolveFirst = resolve;
      rejectFirst = reject;
    });

    await scenario.partsPage.route(issueUrl, async (route) => {
      try {
        const headers = await route.request().allHeaders();
        const response = await route.fetch();
        resolveFirst({
          status: response.status(),
          body: await response.json() as Json,
          key: headers["idempotency-key"] ?? "",
        });
        await route.abort("connectionreset");
      } catch (error) {
        rejectFirst(error);
        await route.abort("failed");
      }
    });

    await scenario.partsPage.getByRole("button", { name: "Issue part" }).click();
    const committed = await firstAttempt;
    expect(committed.status, JSON.stringify(committed.body)).toBe(201);
    expect(committed.key).toMatch(/^[0-9a-f-]{36}$/i);
    await expect(scenario.partsPage.getByRole("alert")).toContainText("Network unavailable");
    await scenario.partsPage.unroute(issueUrl);

    const retryResponsePromise = scenario.partsPage.waitForResponse(
      (response) => response.url().endsWith("/api/v1/inventory/issues/") && response.request().method() === "POST",
    );
    await scenario.partsPage.getByRole("button", { name: "Issue part" }).click();
    const retryResponse = await retryResponsePromise;
    const retryHeaders = await retryResponse.request().allHeaders();
    expect(retryHeaders["idempotency-key"], "the UI naturally retries the original operation key")
      .toBe(committed.key);
    const replayed = await retryResponse.json() as Json;
    expect(retryResponse.status(), JSON.stringify(replayed)).toBe(201);
    expect(id(replayed.transaction, "replayed transaction")).toBe(id(committed.body.transaction, "committed transaction"));

    await expect(scenario.partsPage.getByText("Issue transaction recorded.", { exact: true })).toBeVisible();
    await expect(scenario.partsPage.getByRole("alert")).toHaveCount(0);
    const issue = await assertSingleIssue(scenario.partsPage, scenario, "1.000");
    expect(String(issue.id)).toBe(id(committed.body.transaction, "committed transaction"));

    const auditResult = await getJson(
      scenario.supervisorPage,
      `/api/v1/audit-events/?resource_type=StockTransaction&resource_id=${String(issue.id)}`,
    );
    expect(auditResult.status, JSON.stringify(auditResult.body)).toBe(200);
    expect(rows(auditResult.body.events, "retry audit events").filter((event) => event.action === "stock.issued"))
      .toHaveLength(1);
    expectedAbort.assertOnlyExpectedFailure();
  } finally {
    await diagnostics.attach(testInfo);
    await Promise.all([scenario.partsContext.close(), scenario.supervisorContext.close()]);
  }
});
