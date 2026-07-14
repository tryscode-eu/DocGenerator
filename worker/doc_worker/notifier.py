"""Internal, retryable worker notifications without RabbitMQ dependencies."""

from __future__ import annotations

import json
import re
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .http_client import (
    RedirectRefusedError,
    open_no_redirect,
    validated_callback_url,
    validated_service_token,
    validated_timeout,
)


class CallbackSettings(Protocol):
    harmony_callback_url: str
    harmony_service_token: str
    document_callback_timeout_seconds: int


DOCUMENT_CALLBACK_VERSION = "tryscode.document-callback.v1"
SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SAFE_FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _post_callback(settings: CallbackSettings, payload: dict[str, object]) -> None:
    if not settings.harmony_callback_url:
        raise RuntimeError("Harmony callback failed")
    try:
        callback_url = validated_callback_url(settings.harmony_callback_url)
        timeout = validated_timeout(settings.document_callback_timeout_seconds)
        service_token = validated_service_token(settings.harmony_service_token)
        request = Request(
            callback_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Tryscode-Service": service_token,
                "User-Agent": "TrysCode-Document-Worker",
            },
            method="POST",
        )
        with open_no_redirect(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status < 300:
                raise RuntimeError("Harmony callback failed")
    except (
        HTTPError,
        URLError,
        RedirectRefusedError,
        TimeoutError,
        OSError,
        TypeError,
        ValueError,
    ):
        # Do not retain a redirect Location, URL parser detail, or credential in
        # a chained exception that an outer worker logger could render.
        raise RuntimeError("Harmony callback failed") from None


def notify_success(
    settings: CallbackSettings,
    *,
    job_id: str,
    result_path: str,
    result_size_bytes: int,
    result_sha256: str,
    result_storage_version: str | None = None,
) -> None:
    """Report an opaque storage key only after a worker has published its result."""
    if not isinstance(job_id, str) or not SAFE_JOB_ID.fullmatch(job_id):
        raise RuntimeError("invalid document job identity")
    if (
        not isinstance(result_path, str)
        or result_path.startswith("/")
        or ".." in result_path.split("/")
    ):
        raise RuntimeError("unsafe document result path")
    if (
        type(result_size_bytes) is not int
        or not 5 <= result_size_bytes <= 5 * 1024 * 1024
        or not isinstance(result_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", result_sha256)
        or (
            result_storage_version is not None
            and (
                not isinstance(result_storage_version, str)
                or not result_storage_version
                or len(result_storage_version) > 1024
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in result_storage_version
                )
            )
        )
    ):
        raise RuntimeError("invalid document artifact identity")
    _post_callback(
        settings,
        {
            "callback_version": DOCUMENT_CALLBACK_VERSION,
            "ext_id": job_id,
            "status": "done",
            "progress": 100,
            "result_path": result_path,
            "result_size_bytes": result_size_bytes,
            "result_sha256": result_sha256,
            **(
                {"result_storage_version": result_storage_version}
                if result_storage_version is not None
                else {}
            ),
        },
    )


def notify_failure(
    settings: CallbackSettings,
    *,
    job_id: str,
    failure_code: str,
) -> None:
    """Close a trusted Harmony job after its final retry without leaking diagnostics."""

    if not isinstance(job_id, str) or not SAFE_JOB_ID.fullmatch(job_id):
        raise RuntimeError("invalid document job identity")
    if not isinstance(failure_code, str) or not SAFE_FAILURE_CODE.fullmatch(failure_code):
        raise RuntimeError("invalid document failure code")
    _post_callback(
        settings,
        {
            "ext_id": job_id,
            "status": "failed",
            "progress": 0,
            "error_message": failure_code,
        },
    )
