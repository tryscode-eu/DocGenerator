import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request

import pytest

from doc_worker import http_client
from doc_worker.http_client import RedirectRefusedError, RejectRedirects, open_no_redirect
from doc_worker.notifier import notify_failure, notify_success


class ClosingResponse:
    def __init__(self, status=200):
        self.status = status
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False

    def close(self):
        self.closed = True


def _settings(**overrides):
    values = {
        "harmony_callback_url": "https://harmony.test/api/v1/jobs/callback",
        "harmony_service_token": "internal-test-token",
        "document_callback_timeout_seconds": 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_notifier_uses_one_bounded_post_and_closes_the_response():
    response = ClosingResponse()

    with patch("doc_worker.notifier.open_no_redirect", return_value=response) as opener:
        notify_success(
            _settings(),
            job_id="job-2026-01",
            result_path="documents/subject-kubernetes-job-2026-01.pdf",
            result_size_bytes=1024,
            result_sha256="a" * 64,
        )

    request = opener.call_args.args[0]
    assert request.full_url == "https://harmony.test/api/v1/jobs/callback"
    assert request.get_method() == "POST"
    assert opener.call_args.kwargs == {"timeout": 5.0}
    assert request.get_header("X-tryscode-service") == "internal-test-token"
    assert json.loads(request.data.decode("utf-8")) == {
        "callback_version": "tryscode.document-callback.v1",
        "ext_id": "job-2026-01",
        "status": "done",
        "progress": 100,
        "result_path": "documents/subject-kubernetes-job-2026-01.pdf",
        "result_size_bytes": 1024,
        "result_sha256": "a" * 64,
    }
    assert response.closed


def test_failure_notifier_uses_the_minimal_harmony_contract():
    response = ClosingResponse()

    with patch("doc_worker.notifier.open_no_redirect", return_value=response) as opener:
        notify_failure(
            _settings(),
            job_id="job-2026-01",
            failure_code="document-processing-failed",
        )

    request = opener.call_args.args[0]
    assert json.loads(request.data.decode("utf-8")) == {
        "ext_id": "job-2026-01",
        "status": "failed",
        "progress": 0,
        "error_message": "document-processing-failed",
    }
    assert "callback_version" not in json.loads(request.data.decode("utf-8"))
    assert response.closed


def test_redirect_handler_closes_response_without_exposing_target_or_token():
    request = Request(
        "https://harmony.test/api/v1/jobs/callback",
        headers={"X-Tryscode-Service": "sensitive-token"},
    )
    response = BytesIO()

    with pytest.raises(RedirectRefusedError) as raised:
        RejectRedirects().http_error_307(
            request,
            response,
            307,
            "Temporary Redirect",
            {"Location": "https://attacker.example/collect"},
        )

    assert response.closed
    assert "sensitive-token" not in str(raised.value)
    assert "attacker.example" not in str(raised.value)


@pytest.mark.parametrize(
    "target",
    (
        "file:///etc/passwd",
        "https://user:password@harmony.test/api/v1/jobs/callback",
        "https://harmony.test/api/v1/jobs/callback?next=https://attacker.example",
        "https://harmony.test/api/v1/jobs/%63allback",
        "https://harmony.test/api/v1/jobs/../callback",
        "https://harmony.test//api/v1/jobs/callback",
    ),
)
def test_ambiguous_callback_targets_are_rejected_before_network(target):
    request = Request(target, method="POST")

    with patch.object(http_client._NO_REDIRECT_OPENER, "open") as opener:
        with pytest.raises(ValueError):
            open_no_redirect(request, timeout=5)

    opener.assert_not_called()


def test_plain_http_is_rejected_for_a_remote_host_before_network():
    request = Request("http://harmony.test/api/v1/jobs/callback", method="POST")

    with patch.object(http_client._NO_REDIRECT_OPENER, "open") as opener:
        with pytest.raises(ValueError):
            open_no_redirect(request, timeout=5)

    opener.assert_not_called()


@pytest.mark.parametrize("host", ("localhost", "127.0.0.1", "[::1]"))
def test_plain_http_is_allowed_only_for_explicit_loopback_hosts(host):
    request = Request(f"http://{host}/api/v1/jobs/callback", method="POST")
    response = ClosingResponse()

    with patch.object(
        http_client._NO_REDIRECT_OPENER,
        "open",
        return_value=response,
    ) as opener:
        returned = open_no_redirect(request, timeout=5)

    assert returned is response
    opener.assert_called_once_with(request, timeout=5.0)


@pytest.mark.parametrize("timeout", (True, 0, 31, float("nan"), "5"))
def test_invalid_timeouts_are_rejected_before_network(timeout):
    request = Request("https://harmony.test/api/v1/jobs/callback", method="POST")

    with patch.object(http_client._NO_REDIRECT_OPENER, "open") as opener:
        with pytest.raises(ValueError):
            open_no_redirect(request, timeout=timeout)

    opener.assert_not_called()


def test_notifier_rethrows_redirect_and_configuration_failures_generically():
    cases = (
        (
            _settings(),
            RedirectRefusedError("https://attacker.example/secret-target"),
        ),
        (
            _settings(harmony_callback_url="https://user:secret@harmony.test/callback"),
            None,
        ),
        (
            _settings(document_callback_timeout_seconds=999),
            None,
        ),
    )

    for settings, network_error in cases:
        patcher = patch("doc_worker.notifier.open_no_redirect")
        opener = patcher.start()
        if network_error is not None:
            opener.side_effect = network_error
        try:
            with pytest.raises(RuntimeError) as raised:
                notify_success(
                    settings,
                    job_id="job-2026-01",
                    result_path="documents/subject-kubernetes-job-2026-01.pdf",
                    result_size_bytes=1024,
                    result_sha256="a" * 64,
                )
        finally:
            patcher.stop()

        assert str(raised.value) == "Harmony callback failed"
        assert raised.value.__cause__ is None
        assert "attacker.example" not in str(raised.value)
        assert "secret" not in str(raised.value)


def test_non_success_callback_is_closed_and_reported_generically():
    response = ClosingResponse(status=503)

    with patch("doc_worker.notifier.open_no_redirect", return_value=response):
        with pytest.raises(RuntimeError) as raised:
            notify_success(
                _settings(),
                job_id="job-2026-01",
                result_path="documents/subject-kubernetes-job-2026-01.pdf",
                result_size_bytes=1024,
                result_sha256="a" * 64,
            )

    assert response.closed
    assert str(raised.value) == "Harmony callback failed"


@pytest.mark.parametrize(
    ("size_bytes", "sha256", "storage_version"),
    (
        (True, "a" * 64, None),
        (4, "a" * 64, None),
        (1024, "A" * 64, None),
        (1024, "a" * 63, None),
        (1024, "a" * 64, ""),
        (1024, "a" * 64, "version\nheader"),
    ),
)
def test_invalid_artifact_claims_are_rejected_before_network(
    size_bytes,
    sha256,
    storage_version,
):
    with patch("doc_worker.notifier.open_no_redirect") as opener:
        with pytest.raises(RuntimeError, match="artifact identity"):
            notify_success(
                _settings(),
                job_id="job-2026-01",
                result_path="documents/subject-kubernetes-job-2026-01.pdf",
                result_size_bytes=size_bytes,
                result_sha256=sha256,
                result_storage_version=storage_version,
            )

    opener.assert_not_called()
