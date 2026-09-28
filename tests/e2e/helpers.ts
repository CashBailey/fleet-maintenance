import { expect, type Page, type TestInfo } from "@playwright/test";

export const e2eUser = {
  username: process.env.E2E_USERNAME ?? "fleet.manager@example.com",
  password: process.env.E2E_PASSWORD ?? "DemoPass123!",
};

export function collectBrowserDiagnostics(page: Page) {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource:")) {
      errors.push(`console: ${message.text()}`);
    }
  });
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("requestfailed", (request) => {
    if (request.failure()?.errorText !== "net::ERR_ABORTED") {
      errors.push(`requestfailed: ${request.method()} ${request.url()} ${request.failure()?.errorText ?? "unknown"}`);
    }
  });
  page.on("response", (response) => {
    if (response.status() >= 500) errors.push(`response: ${response.status()} ${response.request().method()} ${response.url()}`);
  });
  return {
    async attach(testInfo: TestInfo) {
      await testInfo.attach("browser-diagnostics", {
        body: Buffer.from(errors.length ? errors.join("\n") : "No browser errors or failed requests."),
        contentType: "text/plain",
      });
    },
    assertClean() {
      expect(errors, "browser console, page, and request errors").toEqual([]);
    },
  };
}

export async function loginThroughUi(page: Page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const username = page.getByLabel(/username|email/i).first();
  const password = page.getByLabel(/password/i).first();
  await expect(username).toBeVisible();
  await username.fill(e2eUser.username);
  await password.fill(e2eUser.password);
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/login/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: /sign in|log in/i }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBeLessThan(300);
  await expect(page).not.toHaveURL(/\/login(?:[/?#]|$)/);
  await expect(page.locator("main")).toBeVisible();
  await expect.poll(async () => (await page.locator("main").innerText()).trim().length).toBeGreaterThan(10);
}
