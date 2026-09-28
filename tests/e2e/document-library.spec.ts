import { createHash, randomUUID } from "node:crypto";

import { expect, test, type BrowserContext, type Download, type Page } from "@playwright/test";

import { collectBrowserDiagnostics, e2eUser } from "./helpers";

type Json = Record<string, unknown>;

const baseURL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:8088";

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

function identifier(value: unknown, label: string): string {
  const result = String(record(value, label).id ?? "");
  expect(result, `${label}.id`).toMatch(/^[0-9a-f-]{36}$/i);
  return result;
}

function panel(page: Page, title: string) {
  return page.getByRole("heading", { level: 2, name: title, exact: true }).locator("..").locator("..");
}

function embeddedTextPdf(text: string): Buffer {
  const escaped = text.replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)");
  const stream = Buffer.from(`BT\n/F1 12 Tf\n72 720 Td\n(${escaped}) Tj\nET\n`, "latin1");
  const objects = [
    Buffer.from("<< /Type /Catalog /Pages 2 0 R >>", "ascii"),
    Buffer.from("<< /Type /Pages /Kids [3 0 R] /Count 1 >>", "ascii"),
    Buffer.from(
      "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
        + "<< /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
      "ascii",
    ),
    Buffer.from("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", "ascii"),
    Buffer.concat([
      Buffer.from(`<< /Length ${stream.length} >>\nstream\n`, "ascii"),
      stream,
      Buffer.from("endstream", "ascii"),
    ]),
  ];
  const chunks = [
    Buffer.from("%PDF-1.4\n%", "ascii"),
    Buffer.from([0xe2, 0xe3, 0xcf, 0xd3]),
    Buffer.from("\n", "ascii"),
  ];
  const offsets = [0];
  let length = chunks.reduce((total, chunk) => total + chunk.length, 0);
  objects.forEach((object, index) => {
    offsets.push(length);
    const serialized = Buffer.concat([
      Buffer.from(`${index + 1} 0 obj\n`, "ascii"),
      object,
      Buffer.from("\nendobj\n", "ascii"),
    ]);
    chunks.push(serialized);
    length += serialized.length;
  });
  const xrefOffset = length;
  const xref = [
    `xref\n0 ${objects.length + 1}\n`,
    "0000000000 65535 f \n",
    ...offsets.slice(1).map((offset) => `${offset.toString().padStart(10, "0")} 00000 n \n`),
    `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\n`,
    `startxref\n${xrefOffset}\n%%EOF\n`,
  ].join("");
  chunks.push(Buffer.from(xref, "ascii"));
  return Buffer.concat(chunks);
}

async function downloadBytes(download: Download): Promise<Buffer> {
  const stream = await download.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream) chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  return Buffer.concat(chunks);
}

async function loginAs(page: Page, username: string): Promise<void> {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
  await page.getByLabel(/^Username/).fill(username);
  await page.getByLabel(/^Password/).fill(e2eUser.password);
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/login/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const response = await responsePromise;
  expect(response.status(), await response.text()).toBe(200);
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toHaveCount(0);
  await expect(page.locator("main")).toBeVisible();
}

async function getJson(page: Page, path: string): Promise<Json> {
  const response = await page.context().request.get(path);
  expect(response.status(), await response.text()).toBe(200);
  return record(await response.json(), path);
}

test("@documents an approved asset PDF is indexed, cited, downloaded, audited, and permission-scoped", async ({ browser, page }, testInfo) => {
  test.setTimeout(120_000);
  const managerDiagnostics = collectBrowserDiagnostics(page);
  let technicianContext: BrowserContext | undefined;
  let technicianDiagnostics: ReturnType<typeof collectBrowserDiagnostics> | undefined;
  let partsContext: BrowserContext | undefined;
  let partsDiagnostics: ReturnType<typeof collectBrowserDiagnostics> | undefined;
  const testData: Json = {};

  try {
    await loginAs(page, e2eUser.username);
    const bootstrap = await getJson(page, "/api/v1/bootstrap/");
    const asset = rows(bootstrap.assets, "bootstrap assets").find((candidate) => candidate.unit_number === "TRK-012");
    expect(asset, "deterministic TRK-012 asset").toBeDefined();
    const assetId = identifier(asset, "TRK-012 asset");

    const suffix = randomUUID().replaceAll("-", "").slice(0, 12).toUpperCase();
    const documentTitle = `E2E torque manual ${suffix}`;
    const fileName = `fleetline-torque-manual-${suffix.toLowerCase()}.pdf`;
    const searchPhrase = `calibrated wrench sentinel ${suffix}`;
    const pdf = embeddedTextPdf(
      `Fleetline torque procedure requires ${searchPhrase} before the truck returns to service.`,
    );
    const pdfSha256 = createHash("sha256").update(pdf).digest("hex");
    const reviewReference = `E2E-SCAN-${suffix}`;
    const reviewNote = "Approved after deterministic E2E content and malware review.";
    testData.asset_id = assetId;
    testData.document_title = documentTitle;
    testData.file_name = fileName;
    testData.pdf_sha256 = pdfSha256;

    await page.goto(`/assets/${assetId}/`, { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { level: 1, name: "TRK-012", exact: true })).toBeVisible();
    const documentsPanel = panel(page, "Truck documents");
    await expect(documentsPanel).toBeVisible();
    await documentsPanel.getByText("Add PDF manual", { exact: true }).click();
    await documentsPanel.getByLabel(/^PDF manual/).setInputFiles({
      name: fileName,
      mimeType: "application/pdf",
      buffer: pdf,
    });
    await documentsPanel.getByLabel(/^Document title/).fill(documentTitle);
    await documentsPanel.getByLabel(/^Category/).selectOption("torque_specification");
    await documentsPanel.getByLabel(/^Manufacturer/).fill("Freightliner");
    await documentsPanel.getByLabel(/^Model or equipment family/).fill("Cascadia");
    await documentsPanel.getByLabel(/^Engine type/).fill("Detroit DD15");
    await documentsPanel.getByLabel(/^Revision/).fill("E2E-1");
    await documentsPanel.getByLabel(/^Source or provenance/).fill("Deterministic Fleetline E2E fixture");
    await documentsPanel.getByLabel(/^License or usage note/).fill("Internal test fixture");

    const uploadRequestPromise = page.waitForRequest(
      (request) => request.url().endsWith("/api/v1/documents/") && request.method() === "POST",
    );
    const uploadResponsePromise = page.waitForResponse(
      (response) => response.url().endsWith("/api/v1/documents/") && response.request().method() === "POST",
    );
    await documentsPanel.getByRole("button", { name: "Upload for review", exact: true }).click();
    const [uploadRequest, uploadResponse] = await Promise.all([uploadRequestPromise, uploadResponsePromise]);
    expect(uploadResponse.status(), await uploadResponse.text()).toBe(201);
    const uploadedDocument = record(
      record(await uploadResponse.json(), "document upload response").document,
      "uploaded document",
    );
    const documentId = identifier(uploadedDocument, "uploaded document");
    const uploadedAttachment = record(uploadedDocument.attachment, "uploaded attachment");
    const attachmentId = identifier(uploadedAttachment, "uploaded attachment");
    const documentKey = String(uploadedAttachment.document_key ?? "");
    const uploadKey = uploadRequest.headers()["idempotency-key"] ?? "";
    expect(uploadKey).toMatch(/^[0-9a-f-]{36}$/i);
    expect(uploadedDocument).toMatchObject({
      asset_id: assetId,
      title: documentTitle,
      category: "torque_specification",
      status: "quarantined",
    });
    expect(uploadedAttachment).toMatchObject({
      id: attachmentId,
      name: fileName,
      document_key: documentKey,
      version: 1,
      sha256: pdfSha256,
      size: pdf.length,
      content_type: "application/pdf",
    });
    testData.document_id = documentId;
    testData.attachment_id = attachmentId;
    testData.document_key = documentKey;
    testData.upload_idempotency_key = uploadKey;
    await expect(page.getByRole("status").filter({ hasText: "Document uploaded and quarantined for security review." })).toBeVisible();
    await documentsPanel.getByText("Add PDF manual", { exact: true }).click();

    const documentRow = page.getByRole("row").filter({ hasText: documentTitle });
    await expect(documentRow).toHaveCount(1);
    await expect(documentRow).toContainText("quarantined");
    await documentRow.getByRole("button", { name: "Review", exact: true }).click();
    const approvalPanel = panel(page, `Approve ${documentTitle}`);
    await approvalPanel.getByLabel(/^Review note/).fill(reviewNote);
    await approvalPanel.getByLabel(/^Security review reference/).fill(reviewReference);
    const approvalRequestPromise = page.waitForRequest(
      (request) => request.url().endsWith(`/api/v1/documents/${documentId}/approve/`)
        && request.method() === "POST",
    );
    const approvalResponsePromise = page.waitForResponse(
      (response) => response.url().endsWith(`/api/v1/documents/${documentId}/approve/`)
        && response.request().method() === "POST",
    );
    await approvalPanel.getByRole("button", { name: "Approve and index", exact: true }).click();
    const [approvalRequest, approvalResponse] = await Promise.all([
      approvalRequestPromise,
      approvalResponsePromise,
    ]);
    expect(approvalResponse.status(), await approvalResponse.text()).toBe(202);
    const approvalKey = approvalRequest.headers()["idempotency-key"] ?? "";
    expect(approvalKey).toMatch(/^[0-9a-f-]{36}$/i);
    expect(record(record(await approvalResponse.json(), "approval response").document, "queued document"))
      .toMatchObject({ id: documentId, status: "queued" });
    testData.approval_idempotency_key = approvalKey;
    await expect(page.getByRole("status").filter({ hasText: "Document approved and queued for text extraction." })).toBeVisible();

    let indexedDocument: Json | undefined;
    await expect.poll(async () => {
      const payload = await getJson(page, `/api/v1/documents/?asset_id=${assetId}`);
      indexedDocument = rows(payload.documents, "asset documents")
        .find((candidate) => candidate.id === documentId);
      return String(indexedDocument?.status ?? "missing");
    }, {
      message: "the real worker should extract and index the embedded PDF text",
      timeout: 60_000,
      intervals: [100, 250, 500, 1_000],
    }).toBe("indexed");
    expect(indexedDocument).toBeDefined();
    expect(Number.isNaN(Date.parse(String(indexedDocument?.processed_at)))).toBe(false);

    await page.reload({ waitUntil: "domcontentloaded" });
    const indexedRow = page.getByRole("row").filter({ hasText: documentTitle });
    await expect(indexedRow).toHaveCount(1);
    await expect(indexedRow).toContainText("indexed");

    technicianContext = await browser.newContext({ baseURL });
    const technicianPage = await technicianContext.newPage();
    technicianDiagnostics = collectBrowserDiagnostics(technicianPage);
    await loginAs(technicianPage, "technician@example.com");
    await technicianPage.goto(`/assets/${assetId}/`, { waitUntil: "domcontentloaded" });
    await expect(technicianPage.getByRole("heading", { level: 1, name: "TRK-012", exact: true })).toBeVisible();
    const technicianDocuments = panel(technicianPage, "Truck documents");
    await expect(technicianDocuments.getByRole("row").filter({ hasText: documentTitle })).toContainText("indexed");
    await expect(technicianDocuments.getByText("Add PDF manual", { exact: true })).toHaveCount(0);
    await expect(technicianDocuments.getByRole("button", { name: "Review", exact: true })).toHaveCount(0);

    const searchForm = technicianDocuments.getByRole("search");
    await searchForm.getByLabel(/^Search this truck/).fill(searchPhrase);
    const searchResponsePromise = technicianPage.waitForResponse(
      (response) => response.url().includes("/api/v1/documents/search/")
        && response.request().method() === "GET",
    );
    await searchForm.getByRole("button", { name: "Search manuals", exact: true }).click();
    const searchResponse = await searchResponsePromise;
    expect(searchResponse.status(), await searchResponse.text()).toBe(200);
    const searchBody = record(await searchResponse.json(), "document search response");
    const result = rows(searchBody.results, "document search results")
      .find((candidate) => candidate.document_id === documentId);
    expect(result, "search result for the uploaded document").toBeDefined();
    expect(result).toMatchObject({ document_id: documentId, page_number: 1 });
    expect(String(result?.excerpt ?? "").toLowerCase()).toContain(searchPhrase.toLowerCase());
    const citation = record(result?.citation, "document citation");
    const downloadUrl = String(citation.download_url ?? "");
    expect(citation).toMatchObject({
      document_title: documentTitle,
      document_key: documentKey,
      version: 1,
      page_number: 1,
      attachment_id: attachmentId,
      download_url: `/api/v1/documents/${documentId}/download/`,
    });

    const resultRow = technicianDocuments.locator("article.workflow-row").filter({ hasText: documentTitle });
    await expect(resultRow).toHaveCount(1);
    await expect(resultRow).toContainText("Page 1");
    await expect(resultRow).toContainText(searchPhrase);
    const openPdf = resultRow.getByRole("link", { name: "Open PDF", exact: true });
    await expect(openPdf).toHaveAttribute("href", downloadUrl);
    const downloadPromise = technicianPage.waitForEvent("download");
    await openPdf.click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe(fileName);
    const downloadedPdf = await downloadBytes(download);
    expect(await download.failure()).toBeNull();
    expect(downloadedPdf.equals(pdf)).toBe(true);
    expect(createHash("sha256").update(downloadedPdf).digest("hex")).toBe(pdfSha256);

    partsContext = await browser.newContext({ baseURL });
    const partsPage = await partsContext.newPage();
    partsDiagnostics = collectBrowserDiagnostics(partsPage);
    await loginAs(partsPage, "parts.clerk@example.com");
    await partsPage.goto(`/assets/${assetId}/`, { waitUntil: "domcontentloaded" });
    await expect(partsPage.getByRole("heading", { level: 1, name: "TRK-012", exact: true })).toBeVisible();
    await expect(partsPage.getByRole("heading", { level: 2, name: "Truck documents", exact: true })).toHaveCount(0);
    const deniedDownload = await partsPage.context().request.get(downloadUrl);
    expect(deniedDownload.status(), await deniedDownload.text()).toBe(403);
    expect(record(await deniedDownload.json(), "denied document response"))
      .toMatchObject({ error: { code: "permission_denied" } });

    const durablePayload = await getJson(page, `/api/v1/documents/?asset_id=${assetId}`);
    const durableDocument = rows(durablePayload.documents, "durable asset documents")
      .find((candidate) => candidate.id === documentId);
    expect(durableDocument).toBeDefined();
    expect(durableDocument).toMatchObject({
      id: documentId,
      asset_id: assetId,
      title: documentTitle,
      status: "indexed",
      security_review_reference: reviewReference,
      attachment: {
        id: attachmentId,
        document_key: documentKey,
        version: 1,
        sha256: pdfSha256,
        size: pdf.length,
        content_type: "application/pdf",
      },
    });

    const documentEvents = rows(
      (await getJson(
        page,
        `/api/v1/audit-events/?resource_type=Document&resource_id=${documentId}`,
      )).events,
      "document audit events",
    );
    expect(documentEvents).toEqual(expect.arrayContaining([
      expect.objectContaining({
        action: "document.created",
        actor: e2eUser.username,
        new_state: "quarantined",
        correlation_id: uploadKey,
        context: expect.objectContaining({ attachment_id: attachmentId, asset_id: assetId }),
      }),
      expect.objectContaining({
        action: "document.security_review_approved",
        actor: e2eUser.username,
        previous_state: "quarantined",
        new_state: "queued",
        correlation_id: approvalKey,
        context: expect.objectContaining({ security_review_reference: reviewReference }),
      }),
      expect.objectContaining({
        action: "document.extracted",
        previous_state: "processing",
        new_state: "indexed",
        source: "worker",
        context: expect.objectContaining({ page_count: 1 }),
      }),
    ]));
    for (const event of documentEvents) {
      expect(Number.isNaN(Date.parse(String(event.occurred_at))), `${String(event.action)} audit timestamp`).toBe(false);
    }
    const attachmentEvents = rows(
      (await getJson(
        page,
        `/api/v1/audit-events/?resource_type=Attachment&resource_id=${attachmentId}`,
      )).events,
      "attachment audit events",
    );
    expect(attachmentEvents).toEqual(expect.arrayContaining([
      expect.objectContaining({
        action: "attachment.created",
        actor: e2eUser.username,
        correlation_id: uploadKey,
        context: expect.objectContaining({
          sha256: pdfSha256,
          document_key: documentKey,
          version: 1,
          technical_document_id: documentId,
        }),
      }),
    ]));

    managerDiagnostics.assertClean();
    technicianDiagnostics.assertClean();
    partsDiagnostics.assertClean();
  } finally {
    await testInfo.attach("document-library-test-data", {
      body: Buffer.from(JSON.stringify(testData, null, 2)),
      contentType: "application/json",
    });
    await managerDiagnostics.attach(testInfo);
    if (technicianDiagnostics) await technicianDiagnostics.attach(testInfo);
    if (partsDiagnostics) await partsDiagnostics.attach(testInfo);
    if (technicianContext) await technicianContext.close();
    if (partsContext) await partsContext.close();
  }
});
