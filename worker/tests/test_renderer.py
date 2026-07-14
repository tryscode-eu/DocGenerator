import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pypdf import PdfReader

from doc_worker.notifier import notify_success
from doc_worker.renderer import (
    DocumentValidationError,
    parse_document,
    parse_subject_document,
    render_document,
    render_subject_pdf,
)
from doc_worker.storage import publish_document

_MISSING = object()


def _valid_moulinette_evidence():
    return {
        "contract_version": "tryscode.review-evidence.v1",
        "report_schema_version": "javamoulinette.report.v1",
        "run_id": "run:2026-07-13.drawer_01",
        "report_sha256": "ab" * 32,
        "mode": "drawer",
        "verdict": {
            "status": "pass",
            "passed": True,
            "reason_code": "required_checks_passed",
            "reason": "Tous les contrôles requis ont réussi.",
        },
        "summary": {
            "total_items": 5,
            "required_items": 3,
            "optional_items": 2,
            "by_status": {"pass": 3, "fail": 1, "not_run": 1, "error": 0},
            "required_by_status": {"pass": 2, "fail": 1, "not_run": 0, "error": 0},
        },
    }


def _valid_review_payload(*, moulinette_evidence=_MISSING):
    payload = {
        "action": "render_review_pdf",
        "job_id": "review-job-2026-01",
        "review_code": "review-41",
        "subject_code": "kubernetes-workloads",
        "subject_name": "Kubernetes Workloads",
        "group_name": "Groupe Orbe",
        "participant_names": ["Camille Dupont", "Alex Martin"],
        "reviewer_name": "Ada Reviewer",
        "reviewed_on": "2026-07-11",
        "is_final": True,
        "verdict": "passed",
        "feedback": "Déploiement clair et reprise sur incident démontrée.",
        "evidence_url": "https://github.com/tryscode/project/actions/41",
        "medal_names": ["Workloads Kubernetes"],
    }
    if moulinette_evidence is not _MISSING:
        payload["moulinette_evidence"] = moulinette_evidence
    return payload


def _set_nested(mapping, path, value):
    target = mapping
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def test_render_subject_pdf(tmp_path: Path):
    payload = {
        "action": "render_subject_pdf",
        "job_id": "job-2026-01",
        "subject_code": "kubernetes-workloads",
        "title": "Kubernetes Workloads",
        "author": "Baptiste RENNESON BOUTARD",
        "sections": [
            {"heading": "Objectif", "body": "Déployer une application de façon déclarative."},
            {"heading": "Livrable", "body": "Un dépôt documenté et un manifeste vérifié."},
        ],
    }
    output = render_subject_pdf(parse_subject_document(payload), tmp_path)
    assert output.name == "subject-kubernetes-workloads-job-2026-01.pdf"
    reader = PdfReader(str(output))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    assert "Kubernetes Workloads" in text
    assert "Baptiste RENNESON BOUTARD" in text


def test_identical_render_is_byte_stable_and_never_replaces_the_first_file(
    tmp_path: Path,
):
    payload = {
        "contract_version": "tryscode.document-render.v1",
        "action": "render_subject_pdf",
        "job_id": "job-2026-stable",
        "subject_code": "kubernetes-workloads",
        "title": "Kubernetes Workloads",
        "author": "Baptiste RENNESON BOUTARD",
        "sections": [{"heading": "Objectif", "body": "Déployer."}],
    }

    first = render_document(parse_document(payload), tmp_path)
    first_bytes = first.read_bytes()
    first_inode = first.stat().st_ino
    second = render_document(parse_document(payload), tmp_path)

    assert second == first
    assert second.read_bytes() == first_bytes
    assert second.stat().st_ino == first_inode


def test_render_fsyncs_the_file_and_create_once_directory_entry(tmp_path: Path):
    payload = {
        "contract_version": "tryscode.document-render.v1",
        "action": "render_subject_pdf",
        "job_id": "job-2026-durable",
        "subject_code": "kubernetes-workloads",
        "title": "Kubernetes Workloads",
        "sections": [{"heading": "Objectif", "body": "Déployer."}],
    }

    with patch("doc_worker.renderer.os.fsync", wraps=os.fsync) as fsync:
        output = render_document(parse_document(payload), tmp_path)

    assert output.is_file()
    assert fsync.call_count >= 2


def test_same_job_key_with_different_content_is_a_fail_closed_collision(tmp_path: Path):
    payload = {
        "action": "render_subject_pdf",
        "job_id": "job-2026-collision",
        "subject_code": "kubernetes-workloads",
        "title": "Original title",
        "sections": [{"heading": "Objectif", "body": "Original body."}],
    }
    output = render_document(parse_document(payload), tmp_path)
    original = output.read_bytes()

    with pytest.raises(RuntimeError, match="collision"):
        render_document(
            parse_document({**payload, "title": "Equivocated title"}),
            tmp_path,
        )

    assert output.read_bytes() == original


def test_unknown_document_contract_version_is_rejected():
    with pytest.raises(DocumentValidationError, match="contract version"):
        parse_document(
            {
                "contract_version": "tryscode.document-render.v999",
                "action": "render_subject_pdf",
            }
        )


def test_render_certificate_attestation_and_student_card(tmp_path: Path):
    payloads = [
        {
            "action": "render_certificate_pdf",
            "job_id": "cert-2026-01",
            "certificate_code": "kubernetes-foundations",
            "certificate_name": "Kubernetes Foundations",
            "learner_name": "Camille Dupont",
            "issued_on": "2026-01-20",
            "verification_code": "TC-VERIFY-2026",
            "rncp_code": "RNCP-TEST",
        },
        {
            "action": "render_attestation_pdf",
            "job_id": "attestation-2026-01",
            "training_code": "kubernetes-foundations",
            "training_name": "Kubernetes Foundations",
            "learner_name": "Camille Dupont",
            "campus_name": "Online",
            "period_start": "2026-01-01",
            "period_end": "2026-01-20",
            "total_hours": 42,
            "issued_on": "2026-01-20",
        },
        {
            "action": "render_student_card_pdf",
            "job_id": "card-2026-01",
            "member_id": "learner-42",
            "learner_name": "Camille Dupont",
            "campus_name": "Online",
            "issued_on": "2026-01-01",
            "valid_until": "2026-12-31",
        },
    ]
    outputs = [render_document(parse_document(payload), tmp_path) for payload in payloads]
    assert [output.name for output in outputs] == [
        "certificate-kubernetes-foundations-cert-2026-01.pdf",
        "attestation-kubernetes-foundations-attestation-2026-01.pdf",
        "student-card-learner-42-card-2026-01.pdf",
    ]
    texts = [PdfReader(str(output)).pages[0].extract_text() for output in outputs]
    assert "CERTIFICAT DE RÉUSSITE" in texts[0]
    assert "ATTESTATION DE FORMATION" in texts[1]
    assert "CARTE APPRENANT" in texts[2]


@pytest.mark.parametrize(
    "payload",
    (
        {
            "contract_version": "tryscode.document-render.v1",
            "action": "render_certificate_pdf",
            "job_id": "cert-stable",
            "certificate_code": "kubernetes-foundations",
            "certificate_name": "Kubernetes Foundations",
            "learner_name": "Camille Dupont",
            "issued_on": "2026-01-20",
            "verification_code": "TC-VERIFY-STABLE",
        },
        {
            "contract_version": "tryscode.document-render.v1",
            "action": "render_attestation_pdf",
            "job_id": "attestation-stable",
            "training_code": "kubernetes-foundations",
            "training_name": "Kubernetes Foundations",
            "learner_name": "Camille Dupont",
            "campus_name": "Online",
            "period_start": "2026-01-01",
            "period_end": "2026-01-20",
            "total_hours": 42,
            "issued_on": "2026-01-20",
        },
        {
            "contract_version": "tryscode.document-render.v1",
            "action": "render_student_card_pdf",
            "job_id": "card-stable",
            "member_id": "learner-42",
            "learner_name": "Camille Dupont",
            "campus_name": "Online",
            "issued_on": "2026-01-01",
            "valid_until": "2026-12-31",
        },
        {
            "contract_version": "tryscode.document-render.v1",
            **_valid_review_payload(),
            "job_id": "review-stable",
            "review_code": "review-stable",
        },
    ),
    ids=("certificate", "attestation", "student-card", "review"),
)
def test_every_document_contract_is_byte_stable_on_replay(payload, tmp_path: Path):
    first = render_document(parse_document(payload), tmp_path)
    first_bytes = first.read_bytes()
    first_inode = first.stat().st_ino

    second = render_document(parse_document(payload), tmp_path)

    assert second.read_bytes() == first_bytes
    assert second.stat().st_ino == first_inode


def test_render_human_review_pdf(tmp_path: Path):
    payload = {
        "action": "render_review_pdf",
        "job_id": "review-job-2026-01",
        "review_code": "review-41",
        "subject_code": "kubernetes-workloads",
        "subject_name": "Kubernetes Workloads",
        "group_name": "Groupe Orbe",
        "participant_names": ["Camille Dupont", "Alex Martin"],
        "reviewer_name": "Ada Reviewer",
        "reviewed_on": "2026-07-11",
        "is_final": True,
        "verdict": "passed",
        "feedback": "Déploiement clair et reprise sur incident démontrée.",
        "evidence_url": "https://github.com/tryscode/project/actions/41",
        "medal_names": ["Workloads Kubernetes"],
    }

    output = render_document(parse_document(payload), tmp_path)

    assert output.name == "review-review-41-review-job-2026-01.pdf"
    text = PdfReader(str(output)).pages[0].extract_text()
    assert "COMPTE RENDU DE REVUE" in text
    assert "Ada Reviewer" in text
    assert "Workloads Kubernetes" in text
    assert "github.com/tryscode/project/actions/41" in text
    assert "décision pédagogique humaine" in text


@pytest.mark.parametrize(
    "moulinette_evidence",
    (_MISSING, None),
    ids=("absent", "explicit-null"),
)
def test_review_evidence_remains_optional(moulinette_evidence, tmp_path: Path):
    document = parse_document(
        _valid_review_payload(moulinette_evidence=moulinette_evidence)
        if moulinette_evidence is not _MISSING
        else _valid_review_payload()
    )

    assert document.moulinette_evidence is None
    output = render_document(document, tmp_path)
    text = "\n".join(page.extract_text() for page in PdfReader(str(output)).pages)
    assert "Preuve technique de moulinette" not in text


def test_render_review_with_bounded_moulinette_evidence(tmp_path: Path):
    evidence = _valid_moulinette_evidence()
    evidence["verdict"]["reason"] = "<b>Contrôle informatif</b> & vérifié"

    document = parse_document(_valid_review_payload(moulinette_evidence=evidence))
    output = render_document(document, tmp_path)
    reader = PdfReader(str(output))
    text = "\n".join(page.extract_text() for page in reader.pages)
    normalized_text = " ".join(text.split())

    assert len(reader.pages) == 2
    assert document.moulinette_evidence is not None
    assert document.moulinette_evidence.run_id == "run:2026-07-13.drawer_01"
    for forbidden_attribute in (
        "items",
        "paths",
        "worker_payload",
        "internal_error",
        "score",
        "gpa",
        "rank",
    ):
        assert not hasattr(document.moulinette_evidence, forbidden_attribute)
    assert "Preuve technique de moulinette" in text
    assert "run:2026-07-13.drawer_01" in text
    assert "drawer" in text
    assert "required_checks_passed" in text
    assert "<b>Contrôle informatif</b> & vérifié" in normalized_text
    assert "total: 5 - requis: 3 - optionnels: 2" in normalized_text
    assert "ab" * 32 in text.replace("\n", "")
    assert "Cette preuve technique est informative." in normalized_text
    assert "l'attribution des médailles restent humaines" in normalized_text
    assert "tryscode.review-evidence.v1" not in text
    assert "javamoulinette.report.v1" not in text
    for forbidden in (
        "worker-payload-secret",
        "/srv/private/Hidden.java",
        "internal-stacktrace-secret",
        "score-secret",
        "GPA-secret",
        "rank-secret",
    ):
        assert forbidden not in text


@pytest.mark.parametrize("mode", ("drawer", "expected"))
@pytest.mark.parametrize("status", ("pass", "fail", "not_run", "error"))
def test_review_evidence_accepts_each_documented_mode_and_status(mode, status):
    evidence = _valid_moulinette_evidence()
    evidence["mode"] = mode
    evidence["verdict"].update({"status": status, "passed": status == "pass"})

    document = parse_document(_valid_review_payload(moulinette_evidence=evidence))

    assert document.moulinette_evidence is not None
    assert document.moulinette_evidence.mode == mode
    assert document.moulinette_evidence.verdict.status == status


def test_review_root_rejects_unknown_fields():
    payload = _valid_review_payload()
    payload["worker_payload"] = {"secret": "worker-payload-secret"}

    with pytest.raises(DocumentValidationError, match="unknown fields"):
        parse_document(payload)


@pytest.mark.parametrize(
    ("path", "field"),
    (
        ((), "items"),
        ((), "paths"),
        ((), "worker_payload"),
        (("verdict",), "internal_error"),
        (("summary",), "score"),
        (("summary",), "GPA"),
        (("summary",), "rank"),
        (("summary", "by_status"), "details"),
        (("summary", "required_by_status"), "items"),
    ),
)
def test_review_evidence_rejects_unknown_nested_fields(path, field):
    evidence = _valid_moulinette_evidence()
    target = evidence
    for key in path:
        target = target[key]
    target[field] = "forbidden-detail-secret"

    with pytest.raises(DocumentValidationError, match="exactly"):
        parse_document(_valid_review_payload(moulinette_evidence=evidence))


@pytest.mark.parametrize(
    ("path", "field"),
    (
        ((), "report_schema_version"),
        (("verdict",), "reason"),
        (("summary",), "optional_items"),
        (("summary", "by_status"), "error"),
        (("summary", "required_by_status"), "not_run"),
    ),
)
def test_review_evidence_requires_every_nested_field(path, field):
    evidence = _valid_moulinette_evidence()
    target = evidence
    for key in path:
        target = target[key]
    del target[field]

    with pytest.raises(DocumentValidationError, match="exactly"):
        parse_document(_valid_review_payload(moulinette_evidence=evidence))


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("summary", "total_items"), True),
        (("summary", "required_items"), False),
        (("summary", "optional_items"), 2.0),
        (("summary", "by_status", "pass"), True),
        (("summary", "required_by_status", "fail"), "1"),
        (("verdict", "passed"), 1),
    ),
)
def test_review_evidence_rejects_bool_and_non_integer_counts(path, value):
    evidence = _valid_moulinette_evidence()
    _set_nested(evidence, path, value)

    with pytest.raises(DocumentValidationError):
        parse_document(_valid_review_payload(moulinette_evidence=evidence))


@pytest.mark.parametrize("case", ("items", "statuses", "required-statuses", "subset"))
def test_review_evidence_rejects_incoherent_sums(case):
    evidence = _valid_moulinette_evidence()
    summary = evidence["summary"]
    if case == "items":
        summary["optional_items"] = 3
    elif case == "statuses":
        summary["by_status"]["not_run"] = 0
    elif case == "required-statuses":
        summary["required_by_status"]["pass"] = 1
    else:
        summary["required_by_status"].update({"pass": 1, "fail": 2})

    with pytest.raises(DocumentValidationError, match="incoherent"):
        parse_document(_valid_review_payload(moulinette_evidence=evidence))


@pytest.mark.parametrize(
    "report_sha256",
    ("ab" * 31, "AB" * 32, "gg" * 32, "ab" * 32 + "0"),
)
def test_review_evidence_requires_exact_lowercase_sha256(report_sha256):
    evidence = _valid_moulinette_evidence()
    evidence["report_sha256"] = report_sha256

    with pytest.raises(DocumentValidationError, match="report_sha256"):
        parse_document(_valid_review_payload(moulinette_evidence=evidence))


@pytest.mark.parametrize(
    ("status", "passed"),
    (("pass", False), ("fail", True), ("not_run", True), ("error", True)),
)
def test_review_evidence_verdict_passed_matches_status(status, passed):
    evidence = _valid_moulinette_evidence()
    evidence["verdict"].update({"status": status, "passed": passed})

    with pytest.raises(DocumentValidationError, match="incoherent"):
        parse_document(_valid_review_payload(moulinette_evidence=evidence))


@pytest.mark.parametrize("run_id", ("", "-bad", "contains space", "x" * 97))
def test_review_evidence_run_id_is_bounded_and_safe(run_id):
    evidence = _valid_moulinette_evidence()
    evidence["run_id"] = run_id

    with pytest.raises(DocumentValidationError, match="run_id"):
        parse_document(_valid_review_payload(moulinette_evidence=evidence))


@pytest.mark.parametrize("reason_code", ("", "/private/path", "x" * 65))
def test_review_evidence_reason_code_is_bounded_and_safe(reason_code):
    evidence = _valid_moulinette_evidence()
    evidence["verdict"]["reason_code"] = reason_code

    with pytest.raises(DocumentValidationError, match="reason_code"):
        parse_document(_valid_review_payload(moulinette_evidence=evidence))


@pytest.mark.parametrize(
    "reason",
    ("", " " * 10, "x" * 501, "x" + " " * 500, "hidden\x00value", "two\nlines"),
)
def test_review_evidence_reason_is_bounded_text(reason):
    evidence = _valid_moulinette_evidence()
    evidence["verdict"]["reason"] = reason

    with pytest.raises(DocumentValidationError, match="reason"):
        parse_document(_valid_review_payload(moulinette_evidence=evidence))


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("contract_version",), "tryscode.review-evidence.v2"),
        (("report_schema_version",), "javamoulinette.report.v2"),
        (("mode",), "automatic"),
        (("verdict", "status"), "passed"),
        (("summary", "total_items"), 0),
        (("summary", "total_items"), 201),
        (("summary", "by_status", "pass"), 201),
    ),
)
def test_review_evidence_rejects_unsupported_versions_modes_statuses_and_bounds(path, value):
    evidence = _valid_moulinette_evidence()
    _set_nested(evidence, path, value)

    with pytest.raises(DocumentValidationError):
        parse_document(_valid_review_payload(moulinette_evidence=evidence))


def test_review_medals_require_a_passed_final_human_review():
    payload = {
        "action": "render_review_pdf",
        "job_id": "review-job-2026-01",
        "review_code": "review-41",
        "subject_code": "kubernetes-workloads",
        "subject_name": "Kubernetes Workloads",
        "group_name": "Groupe Orbe",
        "participant_names": ["Camille Dupont"],
        "reviewer_name": "Ada Reviewer",
        "reviewed_on": "2026-07-11",
        "is_final": False,
        "verdict": "recorded",
        "medal_names": ["Workloads Kubernetes"],
    }

    try:
        parse_document(payload)
    except DocumentValidationError as exc:
        assert "passed final human review" in str(exc)
    else:
        raise AssertionError("Expected permanent validation error")


def test_review_evidence_url_cannot_embed_credentials():
    payload = {
        "action": "render_review_pdf",
        "job_id": "review-job-2026-01",
        "review_code": "review-41",
        "subject_code": "kubernetes-workloads",
        "subject_name": "Kubernetes Workloads",
        "group_name": "Groupe Orbe",
        "participant_names": ["Camille Dupont"],
        "reviewer_name": "Ada Reviewer",
        "reviewed_on": "2026-07-11",
        "is_final": True,
        "verdict": "passed",
        "evidence_url": "https://user:secret@example.test/proof",
        "medal_names": [],
    }

    try:
        parse_document(payload)
    except DocumentValidationError as exc:
        assert "evidence_url" in str(exc)
    else:
        raise AssertionError("Expected permanent validation error")


@pytest.mark.parametrize(
    "evidence_url",
    (
        "https://example.test/proof?token=private",
        "https://example.test/proof#private-fragment",
    ),
)
def test_review_evidence_url_cannot_embed_query_or_fragment(evidence_url):
    payload = {
        "action": "render_review_pdf",
        "job_id": "review-job-2026-01",
        "review_code": "review-41",
        "subject_code": "kubernetes-workloads",
        "subject_name": "Kubernetes Workloads",
        "group_name": "Groupe Orbe",
        "participant_names": ["Camille Dupont"],
        "reviewer_name": "Ada Reviewer",
        "reviewed_on": "2026-07-11",
        "is_final": True,
        "verdict": "passed",
        "evidence_url": evidence_url,
        "medal_names": [],
    }

    with pytest.raises(DocumentValidationError) as exc:
        parse_document(payload)

    assert "evidence_url" in str(exc.value)


def test_document_validation_rejects_invalid_dates():
    payload = {
        "action": "render_attestation_pdf",
        "job_id": "attestation-2026-01",
        "training_code": "kubernetes-foundations",
        "training_name": "Kubernetes Foundations",
        "learner_name": "Camille Dupont",
        "campus_name": "Online",
        "period_start": "2026-01-20",
        "period_end": "2026-01-01",
        "total_hours": 42,
        "issued_on": "2026-01-20",
    }
    try:
        parse_document(payload)
    except DocumentValidationError as exc:
        assert "period_end" in str(exc)
    else:
        raise AssertionError("Expected permanent validation error")


def test_worker_callback_uses_an_opaque_result_name(tmp_path: Path):
    settings = SimpleNamespace(
        harmony_callback_url="https://harmony.test/api/v1/jobs/callback",
        harmony_service_token="internal-test-token",
        document_callback_timeout_seconds=1,
    )

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    payload = {
        "action": "render_subject_pdf",
        "job_id": "job-2026-01",
        "subject_code": "kubernetes-workloads",
        "title": "Kubernetes Workloads",
        "sections": [{"heading": "Objectif", "body": "Déployer."}],
    }
    output = render_document(parse_document(payload), tmp_path)
    with patch("doc_worker.notifier.open_no_redirect", return_value=Response()) as callback:
        notify_success(
            settings,
            job_id="job-2026-01",
            result_path=f"documents/{output.name}",
            result_size_bytes=output.stat().st_size,
            result_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        )
    sent = json.loads(callback.call_args.args[0].data.decode("utf-8"))
    assert sent["result_path"] == f"documents/{output.name}"
    assert not sent["result_path"].startswith("/")
    assert callback.call_args.args[0].get_header("X-tryscode-service") == "internal-test-token"


def test_local_document_storage_uses_a_shared_object_key(tmp_path: Path):
    output = tmp_path / "documents"
    output.mkdir()
    rendered = output / "student-card-learner-42-job-2026-01.pdf"
    rendered.write_bytes(b"%PDF-test")
    settings = SimpleNamespace(
        document_output_dir=output,
        document_storage_mode="local",
        document_storage_prefix="documents",
    )
    published = publish_document(settings, rendered)
    assert published.storage_key == "documents/student-card-learner-42-job-2026-01.pdf"
    assert published.size_bytes == len(b"%PDF-test")
    assert published.sha256 == hashlib.sha256(b"%PDF-test").hexdigest()
    assert published.storage_version is None
