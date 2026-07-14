"""Safe, deterministic renderers for TrysCode pedagogical documents."""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SAFE_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
SAFE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_SECTIONS = 30
MAX_TEXT_LENGTH = 12_000
DEFAULT_ISSUER = "Baptiste RENNESON BOUTARD"
REVIEW_EVIDENCE_CONTRACT_VERSION = "tryscode.review-evidence.v1"
REVIEW_EVIDENCE_REPORT_SCHEMA_VERSION = "javamoulinette.report.v1"
REVIEW_EVIDENCE_STATUSES = ("pass", "fail", "not_run", "error")
MAX_EVIDENCE_RUN_ID_LENGTH = 96
MAX_EVIDENCE_REASON_CODE_LENGTH = 64
MAX_EVIDENCE_REASON_LENGTH = 500
MAX_EVIDENCE_ITEMS = 200
MAX_RENDERED_PDF_BYTES = 5 * 1024 * 1024
DOCUMENT_RENDER_CONTRACT_VERSION = "tryscode.document-render.v1"

REVIEW_PAYLOAD_FIELDS = frozenset(
    {
        "action",
        "job_id",
        "review_code",
        "subject_code",
        "subject_name",
        "group_name",
        "participant_names",
        "reviewer_name",
        "reviewed_on",
        "is_final",
        "verdict",
        "feedback",
        "evidence_url",
        "medal_names",
        "moulinette_evidence",
        "contract_version",
    }
)


class DocumentValidationError(ValueError):
    """Permanent request failure: archive the source message, do not retry it."""


@dataclass(frozen=True)
class Section:
    heading: str
    body: str


@dataclass(frozen=True)
class SubjectDocument:
    job_id: str
    subject_code: str
    title: str
    author: str
    sections: tuple[Section, ...]


@dataclass(frozen=True)
class CertificateDocument:
    job_id: str
    certificate_code: str
    certificate_name: str
    learner_name: str
    issued_on: dt.date
    verification_code: str
    issuer: str
    rncp_code: str | None


@dataclass(frozen=True)
class AttendanceCertificate:
    job_id: str
    training_code: str
    training_name: str
    learner_name: str
    campus_name: str
    period_start: dt.date
    period_end: dt.date
    total_hours: int
    issued_on: dt.date
    issuer: str


@dataclass(frozen=True)
class StudentCardDocument:
    job_id: str
    member_id: str
    learner_name: str
    campus_name: str
    issued_on: dt.date
    valid_until: dt.date


@dataclass(frozen=True)
class EvidenceStatusCounts:
    passed: int
    failed: int
    not_run: int
    error: int

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.not_run + self.error


@dataclass(frozen=True)
class EvidenceVerdict:
    status: str
    passed: bool
    reason_code: str
    reason: str


@dataclass(frozen=True)
class EvidenceSummary:
    total_items: int
    required_items: int
    optional_items: int
    by_status: EvidenceStatusCounts
    required_by_status: EvidenceStatusCounts


@dataclass(frozen=True)
class MoulinetteEvidence:
    contract_version: str
    report_schema_version: str
    run_id: str
    report_sha256: str
    mode: str
    verdict: EvidenceVerdict
    summary: EvidenceSummary


@dataclass(frozen=True)
class ReviewDocument:
    job_id: str
    review_code: str
    subject_code: str
    subject_name: str
    group_name: str
    participant_names: tuple[str, ...]
    reviewer_name: str
    reviewed_on: dt.date
    is_final: bool
    verdict: str
    feedback: str | None
    evidence_url: str | None
    medal_names: tuple[str, ...]
    moulinette_evidence: MoulinetteEvidence | None


RenderableDocument = (
    SubjectDocument
    | CertificateDocument
    | AttendanceCertificate
    | StudentCardDocument
    | ReviewDocument
)


def _required_text(value: Any, name: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise DocumentValidationError(f"{name} must be text")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_length:
        raise DocumentValidationError(f"invalid {name}")
    return cleaned


def _optional_text(value: Any, name: str, *, max_length: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, name, max_length=max_length)


def _optional_http_url(value: Any, name: str) -> str | None:
    url = _optional_text(value, name, max_length=512)
    if url is None:
        return None
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise DocumentValidationError(f"invalid {name}")
    return url


def _safe_identifier(value: Any, name: str, *, pattern: re.Pattern[str] = SAFE_ID) -> str:
    identifier = _required_text(value, name, max_length=64)
    if not pattern.fullmatch(identifier):
        raise DocumentValidationError(f"invalid {name}")
    return identifier


def _date(value: Any, name: str) -> dt.date:
    value = _required_text(value, name, max_length=10)
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise DocumentValidationError(f"invalid {name}") from exc


def _positive_int(value: Any, name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
        raise DocumentValidationError(f"invalid {name}")
    return value


def _bounded_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise DocumentValidationError(f"invalid {name}")
    return value


def _bounded_identifier(value: Any, name: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or not SAFE_EVIDENCE_ID.fullmatch(value)
    ):
        raise DocumentValidationError(f"invalid {name}")
    return value


def _bounded_evidence_text(value: Any, name: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or not all(character.isprintable() for character in value)
    ):
        raise DocumentValidationError(f"invalid {name}")
    cleaned = value.strip()
    if not cleaned:
        raise DocumentValidationError(f"invalid {name}")
    return cleaned


def _require_exact_fields(value: Any, name: str, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise DocumentValidationError(f"{name} must contain exactly the documented fields")
    return value


def _text_list(
    value: Any,
    name: str,
    *,
    max_items: int,
    max_item_length: int,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > max_items or (not value and not allow_empty):
        minimum = 0 if allow_empty else 1
        raise DocumentValidationError(
            f"{name} must contain between {minimum} and {max_items} items"
        )
    return tuple(_required_text(item, f"{name} item", max_length=max_item_length) for item in value)


def parse_subject_document(payload: Any) -> SubjectDocument:
    if not isinstance(payload, dict) or payload.get("action") != "render_subject_pdf":
        raise DocumentValidationError("unsupported document action")
    job_id = _safe_identifier(payload.get("job_id"), "job_id", pattern=SAFE_JOB_ID)
    subject_code = _safe_identifier(payload.get("subject_code"), "subject_code")
    title = _required_text(payload.get("title"), "title", max_length=160)
    author = _required_text(payload.get("author", DEFAULT_ISSUER), "author", max_length=160)
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections or len(raw_sections) > MAX_SECTIONS:
        raise DocumentValidationError("sections must contain between 1 and 30 items")
    sections = []
    for raw_section in raw_sections:
        if not isinstance(raw_section, dict):
            raise DocumentValidationError("each section must be an object")
        sections.append(
            Section(
                heading=_required_text(
                    raw_section.get("heading"), "section heading", max_length=160
                ),
                body=_required_text(
                    raw_section.get("body"), "section body", max_length=MAX_TEXT_LENGTH
                ),
            )
        )
    return SubjectDocument(job_id, subject_code, title, author, tuple(sections))


def _parse_certificate(payload: dict[str, Any]) -> CertificateDocument:
    return CertificateDocument(
        job_id=_safe_identifier(payload.get("job_id"), "job_id", pattern=SAFE_JOB_ID),
        certificate_code=_safe_identifier(
            payload.get("certificate_code"), "certificate_code", pattern=SAFE_JOB_ID
        ),
        certificate_name=_required_text(
            payload.get("certificate_name"), "certificate_name", max_length=160
        ),
        learner_name=_required_text(payload.get("learner_name"), "learner_name", max_length=160),
        issued_on=_date(payload.get("issued_on"), "issued_on"),
        verification_code=_safe_identifier(
            payload.get("verification_code"), "verification_code", pattern=SAFE_JOB_ID
        ),
        issuer=_required_text(payload.get("issuer", DEFAULT_ISSUER), "issuer", max_length=160),
        rncp_code=_optional_text(payload.get("rncp_code"), "rncp_code", max_length=32),
    )


def _parse_attestation(payload: dict[str, Any]) -> AttendanceCertificate:
    period_start = _date(payload.get("period_start"), "period_start")
    period_end = _date(payload.get("period_end"), "period_end")
    if period_end < period_start:
        raise DocumentValidationError("period_end must be after period_start")
    return AttendanceCertificate(
        job_id=_safe_identifier(payload.get("job_id"), "job_id", pattern=SAFE_JOB_ID),
        training_code=_safe_identifier(payload.get("training_code"), "training_code"),
        training_name=_required_text(payload.get("training_name"), "training_name", max_length=160),
        learner_name=_required_text(payload.get("learner_name"), "learner_name", max_length=160),
        campus_name=_required_text(payload.get("campus_name"), "campus_name", max_length=160),
        period_start=period_start,
        period_end=period_end,
        total_hours=_positive_int(payload.get("total_hours"), "total_hours", maximum=10_000),
        issued_on=_date(payload.get("issued_on"), "issued_on"),
        issuer=_required_text(payload.get("issuer", DEFAULT_ISSUER), "issuer", max_length=160),
    )


def _parse_student_card(payload: dict[str, Any]) -> StudentCardDocument:
    issued_on = _date(payload.get("issued_on"), "issued_on")
    valid_until = _date(payload.get("valid_until"), "valid_until")
    if valid_until < issued_on:
        raise DocumentValidationError("valid_until must be after issued_on")
    return StudentCardDocument(
        job_id=_safe_identifier(payload.get("job_id"), "job_id", pattern=SAFE_JOB_ID),
        member_id=_safe_identifier(payload.get("member_id"), "member_id", pattern=SAFE_JOB_ID),
        learner_name=_required_text(payload.get("learner_name"), "learner_name", max_length=160),
        campus_name=_required_text(payload.get("campus_name"), "campus_name", max_length=160),
        issued_on=issued_on,
        valid_until=valid_until,
    )


def _parse_evidence_status_counts(value: Any, name: str) -> EvidenceStatusCounts:
    raw_counts = _require_exact_fields(
        value,
        name,
        frozenset(REVIEW_EVIDENCE_STATUSES),
    )
    return EvidenceStatusCounts(
        passed=_bounded_int(
            raw_counts["pass"],
            f"{name}.pass",
            minimum=0,
            maximum=MAX_EVIDENCE_ITEMS,
        ),
        failed=_bounded_int(
            raw_counts["fail"],
            f"{name}.fail",
            minimum=0,
            maximum=MAX_EVIDENCE_ITEMS,
        ),
        not_run=_bounded_int(
            raw_counts["not_run"],
            f"{name}.not_run",
            minimum=0,
            maximum=MAX_EVIDENCE_ITEMS,
        ),
        error=_bounded_int(
            raw_counts["error"],
            f"{name}.error",
            minimum=0,
            maximum=MAX_EVIDENCE_ITEMS,
        ),
    )


def _parse_moulinette_evidence(value: Any) -> MoulinetteEvidence | None:
    if value is None:
        return None

    evidence = _require_exact_fields(
        value,
        "moulinette_evidence",
        frozenset(
            {
                "contract_version",
                "report_schema_version",
                "run_id",
                "report_sha256",
                "mode",
                "verdict",
                "summary",
            }
        ),
    )
    if evidence["contract_version"] != REVIEW_EVIDENCE_CONTRACT_VERSION:
        raise DocumentValidationError("invalid moulinette_evidence.contract_version")
    if evidence["report_schema_version"] != REVIEW_EVIDENCE_REPORT_SCHEMA_VERSION:
        raise DocumentValidationError("invalid moulinette_evidence.report_schema_version")

    run_id = _bounded_identifier(
        evidence["run_id"],
        "moulinette_evidence.run_id",
        maximum=MAX_EVIDENCE_RUN_ID_LENGTH,
    )
    report_sha256 = evidence["report_sha256"]
    if not isinstance(report_sha256, str) or not SAFE_SHA256.fullmatch(report_sha256):
        raise DocumentValidationError("invalid moulinette_evidence.report_sha256")
    mode = evidence["mode"]
    if not isinstance(mode, str) or mode not in {"drawer", "expected"}:
        raise DocumentValidationError("invalid moulinette_evidence.mode")

    raw_verdict = _require_exact_fields(
        evidence["verdict"],
        "moulinette_evidence.verdict",
        frozenset({"status", "passed", "reason_code", "reason"}),
    )
    status = raw_verdict["status"]
    if not isinstance(status, str) or status not in REVIEW_EVIDENCE_STATUSES:
        raise DocumentValidationError("invalid moulinette_evidence.verdict.status")
    passed = raw_verdict["passed"]
    if not isinstance(passed, bool):
        raise DocumentValidationError("invalid moulinette_evidence.verdict.passed")
    if passed != (status == "pass"):
        raise DocumentValidationError("incoherent moulinette_evidence.verdict")
    verdict = EvidenceVerdict(
        status=status,
        passed=passed,
        reason_code=_bounded_identifier(
            raw_verdict["reason_code"],
            "moulinette_evidence.verdict.reason_code",
            maximum=MAX_EVIDENCE_REASON_CODE_LENGTH,
        ),
        reason=_bounded_evidence_text(
            raw_verdict["reason"],
            "moulinette_evidence.verdict.reason",
            maximum=MAX_EVIDENCE_REASON_LENGTH,
        ),
    )

    raw_summary = _require_exact_fields(
        evidence["summary"],
        "moulinette_evidence.summary",
        frozenset(
            {
                "total_items",
                "required_items",
                "optional_items",
                "by_status",
                "required_by_status",
            }
        ),
    )
    total_items = _bounded_int(
        raw_summary["total_items"],
        "moulinette_evidence.summary.total_items",
        minimum=1,
        maximum=MAX_EVIDENCE_ITEMS,
    )
    required_items = _bounded_int(
        raw_summary["required_items"],
        "moulinette_evidence.summary.required_items",
        minimum=0,
        maximum=MAX_EVIDENCE_ITEMS,
    )
    optional_items = _bounded_int(
        raw_summary["optional_items"],
        "moulinette_evidence.summary.optional_items",
        minimum=0,
        maximum=MAX_EVIDENCE_ITEMS,
    )
    by_status = _parse_evidence_status_counts(
        raw_summary["by_status"],
        "moulinette_evidence.summary.by_status",
    )
    required_by_status = _parse_evidence_status_counts(
        raw_summary["required_by_status"],
        "moulinette_evidence.summary.required_by_status",
    )
    if total_items != required_items + optional_items:
        raise DocumentValidationError("incoherent moulinette_evidence item totals")
    if by_status.total != total_items:
        raise DocumentValidationError("incoherent moulinette_evidence status totals")
    if required_by_status.total != required_items:
        raise DocumentValidationError("incoherent moulinette_evidence required status totals")
    if any(
        required > overall
        for required, overall in (
            (required_by_status.passed, by_status.passed),
            (required_by_status.failed, by_status.failed),
            (required_by_status.not_run, by_status.not_run),
            (required_by_status.error, by_status.error),
        )
    ):
        raise DocumentValidationError("incoherent moulinette_evidence required counts")

    return MoulinetteEvidence(
        contract_version=REVIEW_EVIDENCE_CONTRACT_VERSION,
        report_schema_version=REVIEW_EVIDENCE_REPORT_SCHEMA_VERSION,
        run_id=run_id,
        report_sha256=report_sha256,
        mode=mode,
        verdict=verdict,
        summary=EvidenceSummary(
            total_items=total_items,
            required_items=required_items,
            optional_items=optional_items,
            by_status=by_status,
            required_by_status=required_by_status,
        ),
    )


def _parse_review(payload: dict[str, Any]) -> ReviewDocument:
    if not set(payload).issubset(REVIEW_PAYLOAD_FIELDS):
        raise DocumentValidationError("review payload contains unknown fields")
    is_final = payload.get("is_final")
    if not isinstance(is_final, bool):
        raise DocumentValidationError("is_final must be a boolean")
    verdict = _required_text(payload.get("verdict"), "verdict", max_length=16)
    if verdict not in {"recorded", "passed", "failed", "reschedule"}:
        raise DocumentValidationError("invalid verdict")
    medal_names = _text_list(
        payload.get("medal_names", []),
        "medal_names",
        max_items=100,
        max_item_length=160,
        allow_empty=True,
    )
    if medal_names and (not is_final or verdict != "passed"):
        raise DocumentValidationError("medals require a passed final human review")
    return ReviewDocument(
        job_id=_safe_identifier(payload.get("job_id"), "job_id", pattern=SAFE_JOB_ID),
        review_code=_safe_identifier(
            payload.get("review_code"), "review_code", pattern=SAFE_JOB_ID
        ),
        subject_code=_safe_identifier(payload.get("subject_code"), "subject_code"),
        subject_name=_required_text(payload.get("subject_name"), "subject_name", max_length=160),
        group_name=_required_text(payload.get("group_name"), "group_name", max_length=160),
        participant_names=_text_list(
            payload.get("participant_names"),
            "participant_names",
            max_items=100,
            max_item_length=160,
        ),
        reviewer_name=_required_text(payload.get("reviewer_name"), "reviewer_name", max_length=160),
        reviewed_on=_date(payload.get("reviewed_on"), "reviewed_on"),
        is_final=is_final,
        verdict=verdict,
        feedback=_optional_text(payload.get("feedback"), "feedback", max_length=MAX_TEXT_LENGTH),
        evidence_url=_optional_http_url(payload.get("evidence_url"), "evidence_url"),
        medal_names=medal_names,
        moulinette_evidence=_parse_moulinette_evidence(payload.get("moulinette_evidence")),
    )


def parse_document(payload: Any) -> RenderableDocument:
    if not isinstance(payload, dict):
        raise DocumentValidationError("document payload must be an object")
    contract_version = payload.get("contract_version")
    if contract_version is not None and contract_version != DOCUMENT_RENDER_CONTRACT_VERSION:
        raise DocumentValidationError("unsupported document contract version")
    action = payload.get("action")
    if action == "render_subject_pdf":
        return parse_subject_document(payload)
    if action == "render_certificate_pdf":
        return _parse_certificate(payload)
    if action == "render_attestation_pdf":
        return _parse_attestation(payload)
    if action == "render_student_card_pdf":
        return _parse_student_card(payload)
    if action == "render_review_pdf":
        return _parse_review(payload)
    raise DocumentValidationError("unsupported document action")


def _paragraph_text(value: str) -> str:
    return html.escape(value).replace("\n", "<br/>")


def _date_text(value: dt.date) -> str:
    return value.strftime("%d/%m/%Y")


def _page_decor(canvas, document) -> None:
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#e6ddeb"))
    canvas.line(2 * cm, 1.7 * cm, A4[0] - 2 * cm, 1.7 * cm)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#675c70"))
    canvas.drawString(
        2 * cm, 1.15 * cm, getattr(document, "tryscode_footer", "TrysCode - Document pédagogique")
    )
    canvas.drawRightString(A4[0] - 2 * cm, 1.15 * cm, f"Page {document.page}")
    canvas.restoreState()


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "TrysCodeTitle",
            parent=base["Title"],
            alignment=TA_CENTER,
            fontName="Helvetica-Bold",
            fontSize=24,
            leading=29,
            textColor=colors.HexColor("#32154d"),
            spaceAfter=10,
        ),
        "subtitle": ParagraphStyle(
            "TrysCodeSubtitle",
            parent=base["BodyText"],
            alignment=TA_CENTER,
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#675c70"),
            spaceAfter=20,
        ),
        "heading": ParagraphStyle(
            "TrysCodeHeading",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=18,
            textColor=colors.HexColor("#5b2c87"),
            spaceBefore=12,
            spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "TrysCodeBody",
            parent=base["BodyText"],
            fontSize=10.5,
            leading=15,
            textColor=colors.HexColor("#241b2c"),
            spaceAfter=5,
        ),
        "learner": ParagraphStyle(
            "TrysCodeLearner",
            parent=base["Heading1"],
            alignment=TA_CENTER,
            fontName="Helvetica-Bold",
            fontSize=21,
            leading=26,
            textColor=colors.HexColor("#32154d"),
            spaceAfter=15,
        ),
        "card_title": ParagraphStyle(
            "TrysCodeCardTitle",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=23,
            textColor=colors.white,
            spaceAfter=0,
        ),
        "card_body": ParagraphStyle(
            "TrysCodeCardBody",
            parent=base["BodyText"],
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#2c1e35"),
            spaceAfter=0,
        ),
    }


def _destination(output_dir: Path, kind: str, identifier: str, job_id: str) -> Path:
    destination_dir = Path(output_dir).resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{kind}-{identifier}-{job_id}.pdf"
    if destination.parent != destination_dir:
        raise DocumentValidationError("unsafe output path")
    return destination


def _pdf_identity(path: str | Path) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("rendered document is not a regular file")
        digest = hashlib.sha256()
        size_bytes = 0
        with os.fdopen(descriptor, "rb", closefd=True) as document:
            descriptor = None
            signature = document.read(5)
            if signature != b"%PDF-":
                raise RuntimeError("rendered document has an invalid signature")
            digest.update(signature)
            size_bytes = len(signature)
            while True:
                chunk = document.read(64 * 1024)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > MAX_RENDERED_PDF_BYTES:
                    raise RuntimeError("rendered document exceeds the size limit")
                digest.update(chunk)
        return size_bytes, digest.hexdigest()
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("rendered document cannot be verified") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _build_pdf(destination: Path, *, title: str, author: str, footer: str, story: list) -> Path:
    fd, temporary_name = tempfile.mkstemp(prefix=".render-", suffix=".pdf", dir=destination.parent)
    os.close(fd)
    installed = False
    try:
        pdf = SimpleDocTemplate(
            temporary_name,
            pagesize=A4,
            leftMargin=2.2 * cm,
            rightMargin=2.2 * cm,
            topMargin=2.2 * cm,
            bottomMargin=2.4 * cm,
            title=title,
            author=author,
            invariant=1,
        )
        pdf.tryscode_footer = footer
        pdf.build(story, onFirstPage=_page_decor, onLaterPages=_page_decor)
        with open(temporary_name, "rb") as rendered:
            os.fsync(rendered.fileno())
        temporary_identity = _pdf_identity(temporary_name)
        try:
            os.link(temporary_name, destination, follow_symlinks=False)
            installed = True
        except FileExistsError:
            if _pdf_identity(destination) != temporary_identity:
                raise RuntimeError("document destination collision") from None
            installed = True
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    if installed:
        # The callback is allowed only after the create-once directory entry is
        # durable as well as the PDF contents themselves.
        _fsync_directory(destination.parent)
    return destination


def _metadata_table(rows: list[tuple[str, str]], styles: dict[str, ParagraphStyle]) -> Table:
    values = [
        [
            Paragraph(f"<b>{_paragraph_text(label)}</b>", styles["body"]),
            Paragraph(_paragraph_text(value), styles["body"]),
        ]
        for label, value in rows
    ]
    table = Table(values, colWidths=[4.2 * cm, 10.2 * cm], hAlign="CENTER")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f7f3fa")),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#ded4e5")),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#e7dfeb")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return table


def render_subject_pdf(document_data: SubjectDocument, output_dir: Path) -> Path:
    styles = _styles()
    story = [
        Paragraph(_paragraph_text(document_data.title), styles["title"]),
        Paragraph(
            f"Sujet : {_paragraph_text(document_data.subject_code)}<br/>Auteur : {_paragraph_text(document_data.author)}",
            styles["subtitle"],
        ),
        Spacer(1, 0.2 * cm),
    ]
    for section in document_data.sections:
        story.append(Paragraph(_paragraph_text(section.heading), styles["heading"]))
        story.append(Paragraph(_paragraph_text(section.body), styles["body"]))
    return _build_pdf(
        _destination(output_dir, "subject", document_data.subject_code, document_data.job_id),
        title=document_data.title,
        author=document_data.author,
        footer="TrysCode - Document pédagogique",
        story=story,
    )


def render_certificate_pdf(document_data: CertificateDocument, output_dir: Path) -> Path:
    styles = _styles()
    rncp = (
        f"RNCP {document_data.rncp_code}" if document_data.rncp_code else "Certification TrysCode"
    )
    story = [
        Paragraph("CERTIFICAT DE RÉUSSITE", styles["title"]),
        Paragraph(_paragraph_text(rncp), styles["subtitle"]),
        Spacer(1, 0.6 * cm),
        Paragraph("Ce document atteste que", styles["subtitle"]),
        Paragraph(_paragraph_text(document_data.learner_name), styles["learner"]),
        Paragraph(
            "a satisfait aux exigences pédagogiques de la certification suivante :",
            styles["subtitle"],
        ),
        Paragraph(_paragraph_text(document_data.certificate_name), styles["learner"]),
        Spacer(1, 0.45 * cm),
        _metadata_table(
            [
                ("Délivré le", _date_text(document_data.issued_on)),
                ("Code de vérification", document_data.verification_code),
                ("Émetteur", document_data.issuer),
            ],
            styles,
        ),
    ]
    return _build_pdf(
        _destination(
            output_dir, "certificate", document_data.certificate_code, document_data.job_id
        ),
        title=f"Certificat - {document_data.certificate_name}",
        author=document_data.issuer,
        footer="TrysCode - Certificat de réussite",
        story=story,
    )


def render_attestation_pdf(document_data: AttendanceCertificate, output_dir: Path) -> Path:
    styles = _styles()
    story = [
        Spacer(1, 1.6 * cm),
        Paragraph("ATTESTATION DE FORMATION", styles["title"]),
        Paragraph("Document de suivi pédagogique", styles["subtitle"]),
        Spacer(1, 0.5 * cm),
        Paragraph("TrysCode atteste que", styles["subtitle"]),
        Paragraph(_paragraph_text(document_data.learner_name), styles["learner"]),
        Paragraph("a suivi la formation suivante :", styles["subtitle"]),
        Paragraph(_paragraph_text(document_data.training_name), styles["learner"]),
        Spacer(1, 0.35 * cm),
        _metadata_table(
            [
                ("Campus", document_data.campus_name),
                (
                    "Période",
                    f"du {_date_text(document_data.period_start)} au {_date_text(document_data.period_end)}",
                ),
                ("Volume réalisé", f"{document_data.total_hours} heures"),
                ("Délivrée le", _date_text(document_data.issued_on)),
                ("Émetteur", document_data.issuer),
            ],
            styles,
        ),
    ]
    return _build_pdf(
        _destination(output_dir, "attestation", document_data.training_code, document_data.job_id),
        title=f"Attestation - {document_data.training_name}",
        author=document_data.issuer,
        footer="TrysCode - Attestation de formation",
        story=story,
    )


def render_student_card_pdf(document_data: StudentCardDocument, output_dir: Path) -> Path:
    styles = _styles()
    card_rows = [
        [Paragraph("TRYSCODE", styles["card_title"])],
        [
            Paragraph(
                "CARTE APPRENANT",
                ParagraphStyle(
                    "TrysCodeCardSub",
                    parent=styles["card_body"],
                    fontName="Helvetica-Bold",
                    fontSize=8,
                    textColor=colors.HexColor("#d9c1f2"),
                ),
            )
        ],
        [
            Paragraph(
                f"<b>{_paragraph_text(document_data.learner_name)}</b>",
                ParagraphStyle(
                    "TrysCodeCardName",
                    parent=styles["card_body"],
                    fontName="Helvetica-Bold",
                    fontSize=11,
                    leading=13,
                    textColor=colors.HexColor("#32154d"),
                ),
            )
        ],
        [
            Paragraph(
                f"Campus : {_paragraph_text(document_data.campus_name)}<br/>Identifiant : {_paragraph_text(document_data.member_id)}",
                ParagraphStyle(
                    "TrysCodeCardDetails", parent=styles["card_body"], fontSize=7.5, leading=10
                ),
            )
        ],
        [
            Paragraph(
                f"Émise le {_date_text(document_data.issued_on)} - Valide jusqu'au {_date_text(document_data.valid_until)}<br/>rgpd@tryscode.eu",
                ParagraphStyle(
                    "TrysCodeCardValidity", parent=styles["card_body"], fontSize=6.8, leading=9
                ),
            )
        ],
    ]
    card = Table(
        card_rows,
        colWidths=[8.56 * cm],
        rowHeights=[1.15 * cm, 0.48 * cm, 1.05 * cm, 1.18 * cm, 1.08 * cm],
        hAlign="CENTER",
    )
    card.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 1), colors.HexColor("#32154d")),
                ("BACKGROUND", (0, 2), (-1, -1), colors.HexColor("#faf8fc")),
                ("BOX", (0, 0), (-1, -1), 1.2, colors.HexColor("#5b2c87")),
                ("LINEBELOW", (0, 1), (-1, 1), 0.7, colors.HexColor("#d9c1f2")),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story = [
        Spacer(1, 8 * cm),
        card,
        Spacer(1, 0.25 * cm),
        Paragraph("Format carte standard - découper le long du cadre.", styles["subtitle"]),
    ]
    return _build_pdf(
        _destination(output_dir, "student-card", document_data.member_id, document_data.job_id),
        title=f"Carte apprenant - {document_data.learner_name}",
        author=DEFAULT_ISSUER,
        footer="TrysCode - Carte apprenant",
        story=story,
    )


def _evidence_counts_text(counts: EvidenceStatusCounts) -> str:
    return (
        f"pass: {counts.passed} - fail: {counts.failed} - "
        f"not_run: {counts.not_run} - error: {counts.error}"
    )


def render_review_pdf(document_data: ReviewDocument, output_dir: Path) -> Path:
    styles = _styles()
    verdicts = {
        "recorded": "Consignée",
        "passed": "Validée",
        "failed": "À reprendre",
        "reschedule": "À replanifier",
    }
    story = [
        Paragraph("COMPTE RENDU DE REVUE", styles["title"]),
        Paragraph(
            f"{_paragraph_text(document_data.subject_name)} "
            f"({_paragraph_text(document_data.subject_code)})",
            styles["subtitle"],
        ),
        _metadata_table(
            [
                ("Type", "Revue finale" if document_data.is_final else "Revue de suivi"),
                ("Verdict", verdicts[document_data.verdict]),
                ("Date", _date_text(document_data.reviewed_on)),
                ("Groupe", document_data.group_name),
                ("Participants", ", ".join(document_data.participant_names)),
                ("Reviewer", document_data.reviewer_name),
            ],
            styles,
        ),
        Paragraph("Retour pédagogique", styles["heading"]),
        Paragraph(
            _paragraph_text(document_data.feedback or "Aucun retour textuel consigné."),
            styles["body"],
        ),
    ]
    if document_data.medal_names:
        story.extend(
            [
                Paragraph("Médailles attribuées", styles["heading"]),
                Paragraph(
                    _paragraph_text(", ".join(document_data.medal_names)),
                    styles["body"],
                ),
            ]
        )
    if document_data.evidence_url:
        story.extend(
            [
                Paragraph("Preuve associée", styles["heading"]),
                Paragraph(_paragraph_text(document_data.evidence_url), styles["body"]),
            ]
        )
    story.append(
        Paragraph(
            "Ce compte rendu retrace une décision pédagogique humaine. "
            "Une moulinette peut fournir des éléments techniques, mais ne décide "
            "ni du verdict final ni des médailles.",
            styles["subtitle"],
        )
    )
    if document_data.moulinette_evidence:
        evidence = document_data.moulinette_evidence
        summary = evidence.summary
        story.extend(
            [
                PageBreak(),
                Paragraph("Preuve technique de moulinette", styles["heading"]),
                _metadata_table(
                    [
                        ("Exécution", evidence.run_id),
                        ("Mode", evidence.mode),
                        ("Verdict technique", evidence.verdict.status),
                        (
                            "Motif",
                            f"{evidence.verdict.reason_code} - {evidence.verdict.reason}",
                        ),
                        (
                            "Comptes",
                            f"total: {summary.total_items} - "
                            f"requis: {summary.required_items} - "
                            f"optionnels: {summary.optional_items}",
                        ),
                        ("Statuts - tous", _evidence_counts_text(summary.by_status)),
                        (
                            "Statuts - requis",
                            _evidence_counts_text(summary.required_by_status),
                        ),
                        ("Empreinte SHA-256", evidence.report_sha256),
                    ],
                    styles,
                ),
                Paragraph(
                    "Cette preuve technique est informative. La décision pédagogique "
                    "et l'attribution des médailles restent humaines.",
                    styles["subtitle"],
                ),
            ]
        )
    return _build_pdf(
        _destination(
            output_dir,
            "review",
            document_data.review_code,
            document_data.job_id,
        ),
        title=f"Revue - {document_data.subject_name}",
        author=document_data.reviewer_name,
        footer="TrysCode - Compte rendu de revue",
        story=story,
    )


def render_document(document_data: RenderableDocument, output_dir: Path) -> Path:
    if isinstance(document_data, SubjectDocument):
        return render_subject_pdf(document_data, output_dir)
    if isinstance(document_data, CertificateDocument):
        return render_certificate_pdf(document_data, output_dir)
    if isinstance(document_data, AttendanceCertificate):
        return render_attestation_pdf(document_data, output_dir)
    if isinstance(document_data, StudentCardDocument):
        return render_student_card_pdf(document_data, output_dir)
    return render_review_pdf(document_data, output_dir)
