from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import resource
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from multiprocessing import get_all_start_methods, get_context
from pathlib import Path
from queue import Empty
from typing import Any, cast

import pypdfium2 as pdfium
from assets.models import Asset, AssetType
from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import F
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from pypdf import PdfReader
from pypdf import __version__ as PYPDF_VERSION
from pypdf.errors import PdfReadError
from pypdfium2.version import PYPDFIUM_INFO
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from .exceptions import DomainError
from .models import Attachment, Document, DocumentApplicability, DocumentPage, Organization
from .permissions import has_permission
from .services import audit, emit, idempotent

PUBLISHED_DOCUMENT_STATUSES = {Document.Status.INDEXED, Document.Status.OCR_UNAVAILABLE}
DOCUMENT_APPLICABILITY_LIMIT = 25


class DocumentProcessingError(Exception):
    pass


@dataclass(frozen=True)
class ExtractedPage:
    page_number: int
    text: str
    extraction_method: str
    confidence: float | None
    provenance: dict[str, object]


@dataclass(frozen=True)
class Extraction:
    pages: list[ExtractedPage]
    ocr_unavailable: bool = False
    needs_review: bool = False
    detail: str = ""


def _bounded_text(data: Any, name: str, limit: int, default: str = "") -> str:
    value = default if data.get(name) is None else str(data.get(name) or "").strip()
    if len(value) > limit:
        raise DomainError(f"{name} is too long", code="invalid_document_metadata")
    return value


def _uuid(value: object, name: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise DomainError(f"{name} must be a UUID", code=f"invalid_{name}") from exc


def _document_json(document: Document, *, include_review: bool = False) -> dict[str, object]:
    attachment = document.attachment
    try:
        superseded_by_id = str(document.superseded_by.pk)
    except Document.DoesNotExist:
        superseded_by_id = None
    return {
        "id": str(document.pk),
        "asset_id": str(document.asset_id),
        "asset_unit_number": document.asset.unit_number,
        "title": document.title,
        "category": document.category,
        "manufacturer": document.manufacturer,
        "model": document.model,
        "engine_type": document.engine_type,
        "revision": document.revision,
        "source": document.source,
        "license": document.license,
        "status": document.status,
        "processing_detail": document.processing_detail,
        "attachment": {
            "id": str(attachment.pk),
            "name": attachment.original_name,
            "document_key": str(attachment.document_key),
            "version": attachment.version,
            "sha256": attachment.sha256,
            "size": attachment.size,
            "content_type": attachment.content_type,
        },
        "supersedes_id": str(document.supersedes_id) if document.supersedes_id else None,
        "superseded_by_id": superseded_by_id,
        "processed_at": document.processed_at,
        "created_at": document.created_at,
        "download_url": reverse("document-download", args=[document.pk]),
        "applicability": [
            {
                "asset_id": str(row.asset_id) if row.asset_id else None,
                "asset_type_id": str(row.asset_type_id) if row.asset_type_id else None,
                "make": row.make,
                "model": row.model,
                "engine_type": row.engine_type,
            }
            for row in document.applicability.all()
        ],
        **(
            {
                "security_review_reference": document.security_review_reference,
                "security_reviewed_at": document.security_reviewed_at,
            }
            if include_review
            else {}
        ),
    }


def _document_queryset(organization: Organization):
    return (
        Document.objects.filter(organization=organization)
        .select_related("attachment", "asset", "asset__asset_type")
        .prefetch_related("applicability")
        .order_by("category", "title", "created_at")
    )


def _asset_for_filter(request: Request, value: object) -> Asset:
    asset = get_object_or_404(
        Asset,
        pk=_uuid(value, "asset_id"),
        organization=request.user.organization,
    )
    if not can_access_document_asset(request, asset):
        raise DomainError("Asset access is not allowed", code="permission_denied", status=403)
    return asset


def can_access_document_asset(request: Request, asset: Asset) -> bool:
    """Require source-asset access in addition to the document capability.

    Document managers intentionally retain access to the library even when a
    narrowly scoped management token omits the general asset-reading scope.
    All other readers need asset access (or be the assigned driver) before a
    manual associated with that asset can be exposed.
    """

    return bool(
        has_permission(request.user, "assets.view", request.auth)
        or has_permission(request.user, "documents.manage", request.auth)
        or asset.assigned_driver_id == request.user.pk
    )


def _asset_engine_type(asset: Asset) -> str:
    equipment = asset.specs.get("equipment") if isinstance(asset.specs, dict) else None
    engine = equipment.get("engine") if isinstance(equipment, dict) else None
    return str(engine.get("type") or "").strip() if isinstance(engine, dict) else ""


def _matches_applicability(document: Document, asset: Asset) -> bool:
    if document.asset_id == asset.pk:
        return True
    engine_type = _asset_engine_type(asset).casefold()
    for row in document.applicability.all():
        if row.asset_id and row.asset_id != asset.pk:
            continue
        if row.asset_type_id and row.asset_type_id != asset.asset_type_id:
            continue
        if row.make and row.make.casefold() != asset.make.casefold():
            continue
        if row.model and row.model.casefold() != asset.model.casefold():
            continue
        if row.engine_type and row.engine_type.casefold() != engine_type:
            continue
        return True
    return False


def _visible_documents(request: Request, asset: Asset | None = None) -> list[Document]:
    documents = list(_document_queryset(request.user.organization))
    if not has_permission(request.user, "documents.manage", request.auth):
        # A quarantined/failed replacement must never hide the last version a
        # technician can actually use. Version numbers are immutable and linear,
        # so select the newest *published* revision for every document lineage.
        published_by_key: dict[uuid.UUID, Document] = {}
        for row in documents:
            if row.status not in PUBLISHED_DOCUMENT_STATUSES:
                continue
            current = published_by_key.get(row.attachment.document_key)
            if current is None or row.attachment.version > current.attachment.version:
                published_by_key[row.attachment.document_key] = row
        documents = list(published_by_key.values())
    documents = [row for row in documents if can_access_document_asset(request, row.asset)]
    if asset:
        documents = [row for row in documents if _matches_applicability(row, asset)]
    return documents


def _parse_applicability(data: Any, organization: Organization) -> list[dict[str, object]] | None:
    raw = data.get("applicability")
    if raw in (None, ""):
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DomainError(
                "applicability must be valid JSON", code="invalid_applicability"
            ) from exc
    if not isinstance(raw, list) or len(raw) > DOCUMENT_APPLICABILITY_LIMIT:
        raise DomainError(
            "applicability must contain at most 25 rows", code="invalid_applicability"
        )
    rows: list[dict[str, object]] = []
    allowed = {"asset_id", "asset_type_id", "make", "model", "engine_type"}
    for raw_row in raw:
        if not isinstance(raw_row, dict) or set(raw_row).difference(allowed):
            raise DomainError(
                "applicability contains unsupported fields", code="invalid_applicability"
            )
        row: dict[str, object] = {}
        if raw_row.get("asset_id"):
            row["asset"] = get_object_or_404(
                Asset,
                pk=_uuid(raw_row["asset_id"], "applicability_asset_id"),
                organization=organization,
            )
        if raw_row.get("asset_type_id"):
            row["asset_type"] = get_object_or_404(
                AssetType,
                pk=_uuid(raw_row["asset_type_id"], "applicability_asset_type_id"),
                organization=organization,
            )
        for name, limit in (("make", 100), ("model", 100), ("engine_type", 120)):
            row[name] = _bounded_text(raw_row, name, limit)
        if not any(row.values()):
            raise DomainError("An applicability row needs a target", code="invalid_applicability")
        rows.append(row)
    return rows


def _validate_document_upload(upload: UploadedFile) -> tuple[str, int]:
    # Reuse the attachment MIME/signature validation; manuals merely have a separate size limit.
    from .views import _validate_upload

    return _validate_upload(
        upload,
        max_bytes=settings.DOCUMENT_MAX_BYTES,
        allowed_types={"application/pdf"},
    )


def _previous_document(request: Request) -> Document | None:
    raw = str(request.data.get("supersedes_document_id") or "").strip()
    if not raw:
        return None
    previous = get_object_or_404(
        Document.objects.select_related("attachment", "asset").prefetch_related("applicability"),
        pk=_uuid(raw, "supersedes_document_id"),
        organization=request.user.organization,
    )
    if Document.objects.filter(supersedes=previous).exists():
        raise DomainError(
            "The prior document already has a newer version",
            code="document_not_current",
            status=409,
        )
    return previous


def _create_document(request: Request) -> Response:
    upload = request.FILES.get("file")
    if not upload:
        raise DomainError("A PDF file is required", code="file_required")
    content_type, size = _validate_document_upload(upload)
    previous = _previous_document(request)
    if previous:
        asset = previous.asset
        supplied_asset = request.data.get("asset_id")
        if supplied_asset and _uuid(supplied_asset, "asset_id") != asset.pk:
            raise DomainError(
                "A replacement must retain the same source asset",
                code="document_asset_mismatch",
                status=409,
            )
    else:
        if not request.data.get("asset_id"):
            raise DomainError("asset_id is required", code="asset_id_required")
        asset = _asset_for_filter(request, request.data["asset_id"])
    if not has_permission(request.user, "documents.manage", request.auth):
        raise DomainError(
            "Document management access is not allowed", code="permission_denied", status=403
        )

    title = _bounded_text(request.data, "title", 255, previous.title if previous else upload.name)
    category = _bounded_text(request.data, "category", 80, previous.category if previous else "")
    if not category:
        raise DomainError("category is required", code="document_category_required")
    metadata = {
        "manufacturer": _bounded_text(
            request.data, "manufacturer", 120, previous.manufacturer if previous else ""
        ),
        "model": _bounded_text(request.data, "model", 120, previous.model if previous else ""),
        "engine_type": _bounded_text(
            request.data, "engine_type", 120, previous.engine_type if previous else ""
        ),
        "revision": _bounded_text(
            request.data, "revision", 120, previous.revision if previous else ""
        ),
        "source": _bounded_text(request.data, "source", 255, previous.source if previous else ""),
        "license": _bounded_text(
            request.data, "license", 255, previous.license if previous else ""
        ),
    }
    applicability = _parse_applicability(request.data, request.user.organization)
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    original_name = Path(upload.name).name[:255]
    correlation_id = request.headers.get("Idempotency-Key", "")

    with transaction.atomic():
        if previous:
            previous = (
                Document.objects.select_for_update()
                .select_related("attachment", "asset")
                .get(pk=previous.pk, organization=request.user.organization)
            )
            Attachment.objects.select_for_update().get(pk=previous.attachment_id)
            if Document.objects.filter(supersedes=previous).exists():
                raise DomainError(
                    "The prior document already has a newer version",
                    code="document_not_current",
                    status=409,
                )
        attachment = Attachment(
            organization=request.user.organization,
            uploader=request.user,
            resource_type="asset",
            resource_id=str(asset.pk),
            sensitivity=Attachment.Sensitivity.OPERATIONAL,
            document_key=previous.attachment.document_key if previous else uuid.uuid4(),
            category=category,
            title=title,
            version=previous.attachment.version + 1 if previous else 1,
            supersedes=previous.attachment if previous else None,
            file=upload,
            original_name=original_name,
            content_type=content_type,
            size=size,
            sha256=digest.hexdigest(),
        )
        try:
            attachment.full_clean()
        except ValidationError as exc:
            raise DomainError(
                "Attachment version is invalid",
                code="invalid_document_version",
                details=getattr(exc, "message_dict", None) or exc.messages,
            ) from exc
        attachment.save()
        document = Document(
            organization=request.user.organization,
            attachment=attachment,
            asset=asset,
            title=title,
            category=category,
            supersedes=previous,
            **metadata,
        )
        try:
            document.full_clean()
        except ValidationError as exc:
            raise DomainError(
                "Document metadata is invalid",
                code="invalid_document_metadata",
                details=getattr(exc, "message_dict", None) or exc.messages,
            ) from exc
        document.save()
        if applicability is None and previous:
            # Keep the optional foreign keys as model values while copying a
            # revision.  Serializing a null key as ``asset_id=None`` would
            # incorrectly make the UUID parser reject a perfectly valid
            # make/model/engine applicability rule.
            applicability = []
            for previous_row in previous.applicability.all():
                copied_row: dict[str, object] = {
                    "make": previous_row.make,
                    "model": previous_row.model,
                    "engine_type": previous_row.engine_type,
                }
                if previous_row.asset_id:
                    copied_row["asset"] = previous_row.asset
                if previous_row.asset_type_id:
                    copied_row["asset_type"] = previous_row.asset_type
                applicability.append(copied_row)
        for row in applicability or []:
            row = dict(row)
            if "asset_id" in row:
                row["asset"] = Asset.objects.get(
                    pk=_uuid(row.pop("asset_id"), "applicability_asset_id")
                )
            if "asset_type_id" in row:
                row["asset_type"] = AssetType.objects.get(
                    pk=_uuid(row.pop("asset_type_id"), "applicability_asset_type_id")
                )
            DocumentApplicability.objects.create(
                organization=request.user.organization, document=document, **row
            )
        audit(
            organization=request.user.organization,
            actor=request.user,
            action="attachment.created",
            resource=attachment,
            context={
                "sha256": attachment.sha256,
                "document_key": str(attachment.document_key),
                "version": attachment.version,
                "technical_document_id": str(document.pk),
            },
            correlation_id=correlation_id,
        )
        if previous:
            audit(
                organization=request.user.organization,
                actor=request.user,
                action="attachment.superseded",
                resource=previous.attachment,
                previous_state=str(previous.attachment.version),
                new_state=str(attachment.version),
                context={
                    "document_key": str(attachment.document_key),
                    "superseded_by_id": str(attachment.pk),
                },
                correlation_id=correlation_id,
            )
        audit(
            organization=request.user.organization,
            actor=request.user,
            action="document.created",
            resource=document,
            new_state=Document.Status.QUARANTINED,
            context={"attachment_id": str(attachment.pk), "asset_id": str(asset.pk)},
            correlation_id=correlation_id,
        )
    document = (
        Document.objects.select_related("attachment", "asset")
        .prefetch_related("applicability")
        .get(pk=document.pk)
    )
    return Response({"document": _document_json(document, include_review=True)}, status=201)


@api_view(["GET", "POST"])
@parser_classes([MultiPartParser, FormParser])
def documents(request: Request) -> Response:
    if request.method == "POST":
        if not has_permission(request.user, "documents.manage", request.auth):
            raise DomainError(
                "Document management access is not allowed", code="permission_denied", status=403
            )
        return idempotent(request, lambda: _create_document(request))
    if not has_permission(request.user, "documents.view", request.auth):
        raise DomainError("Document access is not allowed", code="permission_denied", status=403)
    asset = None
    if request.query_params.get("asset_id"):
        asset = _asset_for_filter(request, request.query_params["asset_id"])
    category = str(request.query_params.get("category") or "").strip().casefold()
    rows = _visible_documents(request, asset)
    if category:
        rows = [row for row in rows if row.category.casefold() == category]
    include_review = has_permission(request.user, "documents.manage", request.auth)
    return Response(
        {"documents": [_document_json(row, include_review=include_review) for row in rows]}
    )


@api_view(["POST"])
def approve_document(request: Request, document_id: uuid.UUID) -> Response:
    if not has_permission(request.user, "documents.manage", request.auth):
        raise DomainError(
            "Document management access is not allowed", code="permission_denied", status=403
        )

    def approve() -> Response:
        document = get_object_or_404(
            Document.objects.select_for_update().select_related("attachment", "asset"),
            pk=document_id,
            organization=request.user.organization,
        )
        if Document.objects.filter(supersedes=document).exists():
            raise DomainError(
                "Only the latest document revision can be approved",
                code="document_not_current",
                status=409,
            )
        if document.status != Document.Status.QUARANTINED:
            raise DomainError(
                "Only quarantined documents can be approved",
                code="invalid_document_status",
                status=409,
            )
        review_note = _bounded_text(request.data, "review_note", 1000)
        scan_reference = _bounded_text(request.data, "security_review_reference", 255)
        if not review_note or not scan_reference:
            raise DomainError(
                "review_note and security_review_reference are required",
                code="security_review_required",
            )
        document.status = Document.Status.QUEUED
        document.processing_detail = "Approved for worker extraction."
        document.security_review_note = review_note
        document.security_review_reference = scan_reference
        document.security_reviewed_by = request.user
        document.security_reviewed_at = timezone.now()
        document.save(
            update_fields=[
                "status",
                "processing_detail",
                "security_review_note",
                "security_review_reference",
                "security_reviewed_by",
                "security_reviewed_at",
                "updated_at",
            ]
        )
        audit(
            organization=request.user.organization,
            actor=request.user,
            action="document.security_review_approved",
            resource=document,
            previous_state=Document.Status.QUARANTINED,
            new_state=Document.Status.QUEUED,
            context={"security_review_reference": scan_reference},
            correlation_id=request.headers.get("Idempotency-Key", ""),
        )
        emit(
            organization=request.user.organization,
            event_type="document.extraction_requested",
            resource=document,
            payload={
                "attachment_id": str(document.attachment_id),
                "sha256": document.attachment.sha256,
            },
        )
        return Response({"document": _document_json(document, include_review=True)}, status=202)

    return idempotent(request, approve)


def _snippet(text: str, query: str) -> str:
    terms = [term.casefold() for term in query.split() if term]
    folded = text.casefold()
    position = next((folded.find(term) for term in terms if folded.find(term) >= 0), 0)
    start = max(0, position - 120)
    end = min(len(text), position + 300)
    return f"{'…' if start else ''}{text[start:end].strip()}{'…' if end < len(text) else ''}"


@api_view(["GET"])
def document_search(request: Request) -> Response:
    if not has_permission(request.user, "documents.view", request.auth):
        raise DomainError("Document access is not allowed", code="permission_denied", status=403)
    query_text = str(request.query_params.get("q") or "").strip()
    if len(query_text) < 2:
        raise DomainError("q must contain at least two characters", code="invalid_document_query")
    asset = (
        _asset_for_filter(request, request.query_params["asset_id"])
        if request.query_params.get("asset_id")
        else None
    )
    try:
        limit = min(25, max(1, int(request.query_params.get("limit", "10"))))
    except ValueError as exc:
        raise DomainError("limit must be a number", code="invalid_document_limit") from exc
    document_ids = [row.pk for row in _visible_documents(request, asset)]
    query = SearchQuery(query_text, config="english", search_type="websearch")
    pages = (
        DocumentPage.objects.filter(
            organization=request.user.organization,
            document_id__in=document_ids,
        )
        .select_related("document", "document__attachment", "document__asset")
        .annotate(rank=SearchRank(F("search_vector"), query))
        .filter(search_vector=query)
        .order_by("-rank", "document__title", "page_number")[:limit]
    )
    results = []
    for page in pages:
        document = page.document
        attachment = document.attachment
        results.append(
            {
                "document_id": str(document.pk),
                "page_number": page.page_number,
                "excerpt": _snippet(page.text, query_text),
                "citation": {
                    "document_title": document.title,
                    "document_key": str(attachment.document_key),
                    "version": attachment.version,
                    "page_number": page.page_number,
                    "attachment_id": str(attachment.pk),
                    "download_url": reverse("document-download", args=[document.pk]),
                },
            }
        )
    return Response({"query": query_text, "results": results})


@api_view(["GET"])
def document_download(request: Request, document_id: uuid.UUID) -> FileResponse:
    if not has_permission(request.user, "documents.view", request.auth):
        raise DomainError("Document access is not allowed", code="permission_denied", status=403)
    document = get_object_or_404(
        Document.objects.select_related("attachment", "asset"),
        pk=document_id,
        organization=request.user.organization,
    )
    if not can_access_document_asset(request, document.asset):
        raise DomainError("Document access is not allowed", code="permission_denied", status=403)
    if document.status not in PUBLISHED_DOCUMENT_STATUSES and not has_permission(
        request.user, "documents.manage", request.auth
    ):
        raise DomainError("Document access is not allowed", code="permission_denied", status=403)
    response = FileResponse(
        document.attachment.file.open("rb"),
        content_type=document.attachment.content_type,
        as_attachment=True,
        filename=document.attachment.original_name,
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _run_document_command(command: list[str], deadline: float, *, file_limit_bytes: int) -> bytes:
    """Run an optional local OCR utility with bounded file and CPU output.

    The document worker deliberately does not stream arbitrary child output into
    memory.  POSIX resource limits also make an installed OCR stack unavailable
    on platforms where we cannot safely constrain its generated files.
    """

    remaining = min(settings.DOCUMENT_COMMAND_TIMEOUT_SECONDS, deadline - time.monotonic())
    if remaining <= 0:
        raise DocumentProcessingError("Document processing exceeded its time limit")
    if file_limit_bytes < 1024:
        raise DocumentProcessingError("Document OCR output limit is invalid")

    cpu_seconds = max(1, math.ceil(remaining))

    def apply_limits() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit_bytes, file_limit_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))

    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            result = subprocess.run(  # noqa: S603 - commands and arguments are fixed by this module
                command,
                check=False,
                stdout=stdout,
                stderr=stderr,
                timeout=remaining,
                preexec_fn=apply_limits,  # noqa: PLW1509 - POSIX worker is single-process.
            )
            stdout.seek(0)
            output = stdout.read(file_limit_bytes + 1)
            stderr.seek(0)
            error_output = stderr.read(file_limit_bytes + 1)
    except subprocess.TimeoutExpired as exc:
        raise DocumentProcessingError("A document processing command timed out") from exc
    if len(output) > file_limit_bytes or len(error_output) > file_limit_bytes:
        raise DocumentProcessingError("Document processing exceeded its output limit")
    if result.returncode:
        detail = error_output.decode("utf-8", "replace").strip().replace("\n", " ")[:300]
        raise DocumentProcessingError(f"Document processing failed: {detail or command[0]}")
    return output


def _copy_attachment_pdf(attachment: Attachment, destination: Path, deadline: float) -> None:
    """Copy and verify immutable attachment bytes before parsing untrusted PDF data."""

    digest = hashlib.sha256()
    total = 0
    with attachment.file.open("rb") as source, destination.open("wb") as target:
        while chunk := source.read(1024 * 1024):
            if time.monotonic() >= deadline:
                raise DocumentProcessingError("Document processing exceeded its time limit")
            total += len(chunk)
            if total > settings.DOCUMENT_MAX_BYTES:
                raise DocumentProcessingError("Stored document exceeds the configured size limit")
            digest.update(chunk)
            target.write(chunk)
    if total != attachment.size:
        raise DocumentProcessingError("Stored document size does not match attachment metadata")
    if digest.hexdigest() != attachment.sha256:
        raise DocumentProcessingError("Stored document hash does not match attachment metadata")


def _embedded_pdf_pages(pdf_path: Path, deadline: float) -> tuple[list[ExtractedPage], list[int]]:
    """Extract selectable text with the pinned permissive PDF parser before optional OCR."""

    if time.monotonic() >= deadline:
        raise DocumentProcessingError("Document processing exceeded its time limit")
    try:
        reader = PdfReader(str(pdf_path), strict=False)
    except (OSError, PdfReadError, ValueError) as exc:
        raise DocumentProcessingError("PDF text extraction could not read the document") from exc
    if reader.is_encrypted and reader.decrypt("") == 0:
        raise DocumentProcessingError("Encrypted PDFs cannot be indexed")
    try:
        pages = len(reader.pages)
    except (PdfReadError, ValueError) as exc:
        raise DocumentProcessingError("PDF page count could not be read") from exc
    if not 1 <= pages <= settings.DOCUMENT_MAX_PAGES:
        raise DocumentProcessingError(
            f"Document page count must be between 1 and {settings.DOCUMENT_MAX_PAGES}"
        )
    extracted: list[ExtractedPage] = []
    missing: list[int] = []
    total_text_bytes = 0
    for page_number, page in enumerate(reader.pages, start=1):
        if time.monotonic() >= deadline:
            raise DocumentProcessingError("Document processing exceeded its time limit")
        try:
            text = (page.extract_text() or "").strip()
        except (PdfReadError, ValueError) as exc:
            raise DocumentProcessingError(
                f"PDF text extraction failed on page {page_number}"
            ) from exc
        text_bytes = len(text.encode("utf-8"))
        if text_bytes > settings.DOCUMENT_MAX_PAGE_TEXT_BYTES:
            raise DocumentProcessingError("Extracted text exceeded the configured page limit")
        if text:
            total_text_bytes += text_bytes
            if total_text_bytes > settings.DOCUMENT_MAX_TOTAL_TEXT_BYTES:
                raise DocumentProcessingError(
                    "Extracted text exceeded the configured document limit"
                )
            extracted.append(
                ExtractedPage(
                    page_number=page_number,
                    text=text,
                    extraction_method=DocumentPage.ExtractionMethod.EMBEDDED,
                    confidence=None,
                    provenance={"tool": "pypdf", "version": PYPDF_VERSION},
                )
            )
        else:
            missing.append(page_number)
    return extracted, missing


def ocr_available() -> bool:
    return bool(shutil.which("tesseract"))


def _ocr_runtime_ready(deadline: float) -> bool:
    """Require the binary and English trained data before treating OCR as available."""

    tesseract = shutil.which("tesseract")
    if not tesseract:
        return False
    # A present runtime that times out or fails is a processing error, not the
    # explicit ``ocr_unavailable`` state reserved for a missing binary or
    # missing English trained data. Let the worker mark that error as failed.
    languages = _run_document_command(
        [tesseract, "--list-langs"], deadline, file_limit_bytes=64 * 1024
    )
    return "eng" in {
        line.strip() for line in languages.decode("utf-8", "replace").splitlines() if line.strip()
    }


class _BoundedImageWriter:
    """File-like target that prevents an OCR bitmap from exhausting temporary storage."""

    def __init__(self, target: Any, limit: int):
        self.target = target
        self.limit = limit
        self.written = 0

    def write(self, value: bytes) -> int:
        if self.written + len(value) > self.limit:
            raise OSError("Rendered OCR image exceeded the configured page limit")
        written = self.target.write(value)
        self.written += written
        return written

    def flush(self) -> None:
        self.target.flush()

    def tell(self) -> int:
        return self.target.tell()


def _render_ocr_image(pdf_path: Path, page_number: int, image_path: Path) -> None:
    """Render one PDF page through PDFium without bundling a GPL renderer."""

    document: Any = None
    page: Any = None
    bitmap: Any = None
    image: Image.Image | None = None
    try:
        document = pdfium.PdfDocument(str(pdf_path))
        page = document[page_number - 1]
        width, height = page.get_size()
        if width <= 0 or height <= 0:
            raise DocumentProcessingError("PDF page dimensions are invalid")
        scale = min(150 / 72, math.sqrt(settings.DOCUMENT_MAX_OCR_PIXELS / (width * height)))
        if scale <= 0:
            raise DocumentProcessingError("PDF page dimensions are invalid")
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        with image_path.open("wb") as target:
            image.save(
                cast(Any, _BoundedImageWriter(target, settings.DOCUMENT_MAX_OCR_IMAGE_BYTES)),
                format="PNG",
                optimize=False,
            )
    except DocumentProcessingError:
        raise
    except Exception as exc:
        raise DocumentProcessingError(
            f"PDF page {page_number} could not be rendered for OCR"
        ) from exc
    finally:
        if image is not None:
            image.close()
        if bitmap is not None:
            bitmap.close()
        if page is not None:
            page.close()
        if document is not None:
            document.close()


def _ocr_page(
    pdf_path: Path,
    page_number: int,
    work_dir: Path,
    deadline: float,
) -> ExtractedPage | None:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return None
    image_path = work_dir / f"page-{page_number}.png"
    try:
        _render_ocr_image(pdf_path, page_number, image_path)
        if (
            not image_path.exists()
            or image_path.stat().st_size > settings.DOCUMENT_MAX_OCR_IMAGE_BYTES
        ):
            raise DocumentProcessingError("Rendered OCR image exceeded the configured page limit")
        tsv = _run_document_command(
            [tesseract, str(image_path), "stdout", "-l", "eng", "tsv"],
            deadline,
            file_limit_bytes=settings.DOCUMENT_MAX_OCR_TSV_BYTES,
        )
        words: list[str] = []
        confidences: list[float] = []
        for row in csv.DictReader(io.StringIO(tsv.decode("utf-8", "replace")), delimiter="\t"):
            text = str(row.get("text") or "").strip()
            if row.get("level") != "5" or not text:
                continue
            words.append(text)
            try:
                confidence = float(str(row.get("conf") or "-1"))
            except ValueError:
                confidence = -1
            if confidence >= 0:
                confidences.append(confidence)
        text = " ".join(words).strip()
        if not text:
            return None
        if len(text.encode()) > settings.DOCUMENT_MAX_PAGE_TEXT_BYTES:
            raise DocumentProcessingError("Extracted text exceeded the configured page limit")
        return ExtractedPage(
            page_number=page_number,
            text=text,
            extraction_method=DocumentPage.ExtractionMethod.OCR,
            confidence=round(sum(confidences) / len(confidences), 2) if confidences else None,
            provenance={
                "tool": "tesseract",
                "language": "eng",
                "renderer": "pypdfium2",
                "renderer_version": str(PYPDFIUM_INFO),
            },
        )
    finally:
        # Keep the temporary footprint to one rendered page even for a large
        # scanned manual.  The source PDF and all extracted text remain
        # immutable; this bitmap is only an OCR transport artifact.
        image_path.unlink(missing_ok=True)


def _ensure_document_text_limit(pages: list[ExtractedPage]) -> None:
    if (
        sum(len(page.text.encode("utf-8")) for page in pages)
        > settings.DOCUMENT_MAX_TOTAL_TEXT_BYTES
    ):
        raise DocumentProcessingError("Extracted text exceeded the configured document limit")


def _extract_pdf_file(pdf_path: Path, work_dir: Path, deadline: float) -> Extraction:
    """Parse one already-verified PDF in the isolated extractor process."""

    extracted, missing = _embedded_pdf_pages(pdf_path, deadline)
    if not missing:
        return Extraction(pages=extracted)
    if not _ocr_runtime_ready(deadline):
        return Extraction(
            pages=extracted,
            ocr_unavailable=True,
            detail=(
                f"{len(missing)} page(s) have no embedded text; OCR needs the "
                "configured tesseract worker runtime with English trained data."
            ),
        )
    unreadable = []
    for page_number in missing:
        page = _ocr_page(pdf_path, page_number, work_dir, deadline)
        if page:
            extracted.append(page)
            _ensure_document_text_limit(extracted)
        else:
            unreadable.append(page_number)
    extracted.sort(key=lambda page: page.page_number)
    if unreadable:
        return Extraction(
            pages=extracted,
            needs_review=True,
            detail=f"{len(unreadable)} page(s) produced no readable OCR text and need review.",
        )
    return Extraction(pages=extracted)


def _extract_pdf_child(pdf_path: str, work_dir: str, deadline: float, result_queue: Any) -> None:
    """Run untrusted PDF parsing outside the database-owning worker process."""

    try:
        # This child must never use Django's inherited database connection:
        # calling ``connections.close_all()`` after fork would send a protocol
        # close/rollback over the parent's active transaction. It performs
        # file-only extraction and exits immediately after returning its result.
        # Its process group lets the parent terminate it and any OCR subprocess.
        os.setsid()
        cpu_seconds = max(1, math.ceil(max(1, deadline - time.monotonic())))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        extraction = _extract_pdf_file(Path(pdf_path), Path(work_dir), deadline)
    except DocumentProcessingError as exc:
        result_queue.put(("error", str(exc)[:1000]))
    except BaseException as exc:  # noqa: BLE001 - report child crashes to the durable worker
        result_queue.put(
            ("error", f"Document extraction failed: {type(exc).__name__}: {exc}"[:1000])
        )
    else:
        result_queue.put(("success", extraction))


def _stop_extractor_child(process: Any) -> None:
    if not process.is_alive():
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (AttributeError, OSError):
        process.terminate()
    process.join(timeout=0.5)
    if process.is_alive():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (AttributeError, OSError):
            process.kill()
        process.join(timeout=0.5)


def _extract_pdf_in_child(pdf_path: Path, work_dir: Path, deadline: float) -> Extraction:
    if "fork" not in get_all_start_methods():
        raise DocumentProcessingError(
            "Document extraction requires a POSIX worker isolation runtime"
        )
    context = get_context("fork")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_extract_pdf_child,
        args=(str(pdf_path), str(work_dir), deadline, result_queue),
    )
    process.start()
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DocumentProcessingError("Document processing exceeded its time limit")
        try:
            outcome, payload = result_queue.get(timeout=remaining)
        except Empty as exc:
            raise DocumentProcessingError("Document processing exceeded its time limit") from exc
        if outcome != "success":
            raise DocumentProcessingError(str(payload))
        if not isinstance(payload, Extraction):
            raise DocumentProcessingError("Document extraction returned an invalid result")
        return payload
    finally:
        _stop_extractor_child(process)
        result_queue.close()
        result_queue.join_thread()


def _extract_pdf_pages(attachment: Attachment) -> Extraction:
    if attachment.content_type != "application/pdf":
        raise DocumentProcessingError("Only PDF technical documents can be extracted")
    deadline = time.monotonic() + settings.DOCUMENT_PROCESS_TIMEOUT_SECONDS
    with tempfile.TemporaryDirectory(prefix="fleetline-document-") as directory:
        work_dir = Path(directory)
        pdf_path = work_dir / "source.pdf"
        _copy_attachment_pdf(attachment, pdf_path, deadline)
        return _extract_pdf_in_child(pdf_path, work_dir, deadline)


def process_document(document_id: uuid.UUID, organization_id: uuid.UUID) -> None:
    """Run by the outbox worker; retries are harmless because pages are revision-bound."""

    document = (
        # security_reviewed_by is nullable, so lock only the document row. PostgreSQL
        # cannot lock the nullable side of the select_related outer join.
        Document.objects.select_for_update(of=("self",))
        .select_related("attachment", "asset", "security_reviewed_by")
        .filter(pk=document_id, organization_id=organization_id)
        .first()
    )
    if not document or document.status != Document.Status.QUEUED:
        return
    document.status = Document.Status.PROCESSING
    document.processing_detail = "Extracting embedded PDF text."
    document.save(update_fields=["status", "processing_detail", "updated_at"])
    try:
        extraction = _extract_pdf_pages(document.attachment)
    except DocumentProcessingError as exc:
        document.status = Document.Status.FAILED
        document.processing_detail = str(exc)[:1000]
        document.processed_at = timezone.now()
        document.save(update_fields=["status", "processing_detail", "processed_at", "updated_at"])
        audit(
            organization=document.organization,
            actor=document.security_reviewed_by,
            action="document.extraction_failed",
            resource=document,
            previous_state=Document.Status.PROCESSING,
            new_state=Document.Status.FAILED,
            context={"detail": document.processing_detail},
            source="worker",
        )
        return
    if DocumentPage.objects.filter(document=document).exists():
        raise RuntimeError("Document extraction would overwrite immutable page text")
    DocumentPage.objects.bulk_create(
        [
            DocumentPage(
                organization=document.organization,
                document=document,
                page_number=page.page_number,
                text=page.text,
                extraction_method=page.extraction_method,
                confidence=page.confidence,
                provenance={**page.provenance, "attachment_sha256": document.attachment.sha256},
            )
            for page in extraction.pages
        ]
    )
    if extraction.ocr_unavailable:
        final_status = Document.Status.OCR_UNAVAILABLE
    elif extraction.needs_review:
        final_status = Document.Status.NEEDS_REVIEW
    else:
        final_status = Document.Status.INDEXED
    document.status = final_status
    document.processing_detail = extraction.detail
    document.processed_at = timezone.now()
    document.save(update_fields=["status", "processing_detail", "processed_at", "updated_at"])
    audit(
        organization=document.organization,
        actor=document.security_reviewed_by,
        action="document.extracted",
        resource=document,
        previous_state=Document.Status.PROCESSING,
        new_state=final_status,
        context={"page_count": len(extraction.pages), "detail": extraction.detail},
        source="worker",
    )
    emit(
        organization=document.organization,
        event_type=(
            "document.indexed"
            if final_status == Document.Status.INDEXED
            else "document.processing_completed"
        ),
        resource=document,
        payload={
            "attachment_id": str(document.attachment_id),
            "page_count": len(extraction.pages),
            "status": final_status,
        },
    )


def process_document_event(resource_id: str, organization_id: uuid.UUID) -> None:
    try:
        document_id = uuid.UUID(resource_id)
    except ValueError as exc:
        raise DocumentProcessingError(
            "Document extraction event has an invalid resource ID"
        ) from exc
    process_document(document_id, organization_id)
