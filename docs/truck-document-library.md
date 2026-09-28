# Truck document library

Fleetline implements a searchable, revisioned technical-document library for
truck manuals, specifications, torque references, parts manuals,
troubleshooting guides, wiring diagrams, and manufacturer service material. It
extends the existing immutable `Attachment` record; it does not create a second
file store or silently alter source bytes.

## Document record and applicability

Each technical-document revision has one immutable attachment plus a `Document`
record with title, category, manufacturer, model, engine type, revision, source,
and license metadata. An upload has one source asset. Optional applicability
rows make the same document discoverable for a specific asset, asset type,
make/model, or engine type. This supports a manual first collected from one
truck while retaining an explicit, auditable statement of which comparable
equipment it applies to.

Technical documents are operational material. The database rejects linking a
financial-classified attachment into the document library, so a cost sheet or
commercial attachment cannot become visible through technician document search
or download routes.

Replacement manuals are created only through `POST /api/v1/documents/` with
`supersedes_document_id`. The attachment lineage keeps its `document_key` and
increments its version. The old revision, source bytes, page text, and audit
events remain retained. The generic attachment endpoint hides technical
documents and rejects attempts to supersede one, so a manual cannot be replaced
without document metadata and review.

The database enforces organization matching between the document, attachment,
source asset, reviewer, applicability rows, and extracted pages. It also blocks
destructive metadata edits, page edits, and deletes. A corrected manual is a
new revision, not an update in place.

## Review and processing lifecycle

An authorized document manager uploads a PDF, which starts as `quarantined`.
Before processing, the manager must enter both a review note and a
`security_review_reference` identifying the organization’s malware/security
scan. Fleetline records that attestation; it does not claim to perform malware
scanning itself.

Approval appends an audit event and an outbox event. The real database-backed
worker then uses these states:

| State | Meaning and visibility |
| --- | --- |
| `quarantined` | Awaiting security review; document managers only |
| `queued` / `processing` | Approved and awaiting/running extraction; document managers only |
| `indexed` | Every page was indexed from embedded text and/or OCR; authorized readers can list, search, and download it |
| `ocr_unavailable` | One or more scanned pages had no embedded text and the worker lacks Tesseract; any embedded-text pages are still indexed and the status states the limitation; authorized readers can download it |
| `needs_review` | OCR executed but one or more pages produced no usable text; document managers resolve it with a replacement revision |
| `failed` | Parsing, integrity, encrypted-PDF, limit, or processing failure; document managers can see the exact bounded reason |

For a technician, a pending or failed replacement never hides the last usable
published revision. Managers can inspect the complete revision history. Only a
newest revision can be approved, preventing a stale revision from becoming the
visible result after a replacement was uploaded.

## Extraction and search

The worker copies the immutable attachment to a private temporary directory,
verifies its recorded size and SHA-256, then runs untrusted PDF parsing in a
short-lived POSIX child process which the worker terminates at the configured
document deadline. It applies page, time, image, per-page/total text, and output
ceilings. It first extracts selectable PDF text with pinned `pypdf`.
For pages without text, the production container renders just that page with
PDFium through pinned `pypdfium2` and OCRs the rendered image with the local
Tesseract English engine. The original PDF is never rewritten.

Each retained `DocumentPage` records the document, organization, page number,
text, extraction method (`embedded` or `ocr`), optional OCR confidence, and
provenance. Provenance includes the extractor/renderer versions and source
attachment SHA-256. PostgreSQL maintains an English `tsvector` with a GIN index
for page-level full-text search. This is intentionally a deterministic
database-search layer, not a generic vector store or a hidden LLM dependency.

Scanned-document OCR is a real deployed capability, not an embedded-text
substitute. The checked-in Dockerfile installs `tesseract-ocr` and
`tesseract-ocr-eng`; the Python renderer is PDFium rather than Poppler. A native
worker must provision a maintained Tesseract binary plus English trained data
on its `PATH` (and `TESSDATA_PREFIX` when the data is in a nonstandard path).
If that runtime is unavailable, the worker reports `ocr_unavailable` explicitly
rather than declaring an image-only manual searchable.

The optional local OCR process is constrained by CPU timeout and file/output
limits. The checked-in worker mounts source media read-only and uses a bounded
writable temporary directory and a memory/CPU cgroup limit. The existing outbox
worker also delivers approved outbound webhooks, so Compose does not claim a
blanket no-egress OCR sandbox; production egress policy must explicitly preserve
only the organization-approved webhook route. The extractor itself makes no
network calls. The deployment image supplies the supported OCR path;
substituting Poppler, Ghostscript, OCRmyPDF, or another renderer requires the
dependency-license and security review described in `docs/dependency-policy.md`.

## Authorization and grounded retrieval

`documents.view` allows a same-organization user with source-asset access to
list published applicable manuals, search indexed pages, and download the
original PDF. `documents.manage` allows upload, security review, and
pending/history visibility, including source-asset access for that library
management purpose. The standard
roles are technician (view), shop supervisor (view/manage), purchasing manager
(view), fleet manager (view/manage), and system administrator (view/manage).
Drivers and parts clerks do not receive the capability by default. Authorization
is checked server-side for every route and download; a cross-organization ID
returns no usable document.

`GET /api/v1/documents/search/?q=...` is the current grounded retrieval API. A
result contains an authorized excerpt and a stable citation with document title,
document lineage/version, page number, attachment ID, and an authorized download
URL. Any future technician-assistance integration must retrieve through this
permission-filtered API, present these citations to the technician, and keep all
maintenance mutations behind an explicit human action. Fleetline currently
ships no external LLM provider, no autonomous troubleshooting decision maker,
and no LLM mutation path.

## Operational validation still required

Before relying on generated search answers during maintenance work, collect a
representative controlled corpus of the actual truck manuals, especially scans.
Measure OCR accuracy for torque values, part numbers, wiring identifiers, tables,
and faded diagrams; record document ownership/license; validate applicability
against the truck roster; and retain a technician’s source-PDF/page review step.
See `docs/validation-assumptions.md` for the outstanding fleet-specific evidence.
