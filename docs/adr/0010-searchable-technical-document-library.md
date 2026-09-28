# ADR 0010: Searchable technical document library on immutable attachments

- Date: 2026-09-04

Fleetline needs truck manuals and older scanned documentation to be searchable
without replacing the existing attachment/audit model or introducing an
unbounded LLM platform.

We retain immutable `Attachment` bytes and version lineage as the source of
truth. A `Document` adds technical metadata, source asset, review state, and
optional applicability rows. `DocumentPage` stores append-only page text,
method, confidence, provenance, and a PostgreSQL full-text projection. Database
guards enforce tenant alignment and forbid document/page mutation or deletion;
replacement creates a new attachment/document revision.

Text extraction is embedded-PDF first using pinned BSD-3 `pypdf`. For scanned
pages the supported production image renders with pinned PDFium bindings
(`pypdfium2`, Apache-2.0 OR BSD-3-Clause with shipped PDFium notices) and OCRs
with local Tesseract plus English data (Apache-2.0). This deliberately avoids
bundling Poppler, Ghostscript, OCRmyPDF, or another GPL/AGPL renderer. PDFium
and image output are bounded by configured pages, pixels, sizes, and processing
time; the local OCR child also has CPU and output-file limits. If the native OCR
runtime is absent, the record reaches explicit `ocr_unavailable`, never a false
claim that an image-only PDF is searchable.

Document upload remains quarantined until a manager records a malware/security
review reference. Extraction is requested through the existing transactional
outbox and performed by the existing worker. Readers see only the newest
published revision in a lineage, while managers see pending/history. Search is
authorization-filtered before PostgreSQL full-text matching and returns excerpts
with document/version/page/download citations.

No external LLM is added in this MVP. A future assistant must use the
permission-filtered retrieval endpoint, return the stored citations, and never
mutate maintenance records without a separate authorized human action.
