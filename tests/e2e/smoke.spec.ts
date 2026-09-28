import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import { collectBrowserDiagnostics, e2eUser, loginThroughUi } from "./helpers";

test("@smoke production application signs in and serves a healthy stack", async ({ page, request }, testInfo) => {
  const diagnostics = collectBrowserDiagnostics(page);
  try {
    const healthResponse = await request.get("/health/ready");
    expect(healthResponse.status()).toBe(200);
    expect(await healthResponse.json()).toMatchObject({
      status: "ok",
      checks: { application: "ok", database: "ok", frontend: "ok", worker: "ok" },
    });

    await loginThroughUi(page);
    await expect(page.getByText("Fleetline", { exact: true }).first()).toBeVisible();
    await expect(page.getByRole("heading", { level: 1, name: "Today", exact: true })).toBeVisible();
    const meResponse = await page.context().request.get("/api/v1/auth/me/");
    expect(meResponse.status()).toBe(200);
    expect(await meResponse.json()).toMatchObject({
      user: {
        username: e2eUser.username,
        organization_id: expect.stringMatching(/^[0-9a-f-]{36}$/i),
        roles: expect.arrayContaining(["fleet_manager"]),
        permissions: expect.arrayContaining(["assets.manage", "maintenance.manage", "reports.all"]),
        navigation: expect.arrayContaining([
          expect.objectContaining({ label: "Today", href: "/" }),
          expect.objectContaining({ label: "Work", href: "/work-orders" }),
        ]),
      },
    });

    const violations = await new AxeBuilder({ page }).analyze();
    expect(violations.violations, "axe accessibility violations").toEqual([]);
    diagnostics.assertClean();
  } finally {
    await diagnostics.attach(testInfo);
  }
});
