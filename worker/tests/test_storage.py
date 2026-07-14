import hashlib
import os
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from doc_worker.storage import publish_document


def _settings(output: Path):
    return SimpleNamespace(
        document_output_dir=output,
        document_storage_mode="s3",
        document_storage_prefix="documents",
        s3_prefix="private",
        s3_endpoint_url="https://s3.example.test",
        s3_bucket="tryscode",
        s3_access_key="test-access-key",
        s3_secret_key="test-secret-key",
        s3_region="eu-west-3",
        s3_connect_timeout_seconds=3.0,
        s3_read_timeout_seconds=10.0,
        s3_max_attempts=2,
    )


class _StoredBody(BytesIO):
    pass


class _MemoryS3:
    def __init__(self, *, stored: bytes | None = None, version: str | None = "v1"):
        self.stored = stored
        self.version = version
        self.put_calls = []
        self.get_calls = []
        self.put_error = None
        self.last_body = None

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        self.last_body = kwargs["Body"]
        if self.put_error is not None:
            raise self.put_error
        self.stored = bytes(kwargs["Body"])
        return {"VersionId": self.version} if self.version is not None else {}

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs)
        assert self.stored is not None
        response = {"Body": _StoredBody(self.stored)}
        if self.version is not None:
            response["VersionId"] = self.version
        return response


def _precondition_failed() -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "PreconditionFailed"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        },
        "PutObject",
    )


def test_s3_upload_uses_the_verified_snapshot_and_exact_version(tmp_path):
    output = tmp_path / "documents"
    output.mkdir()
    rendered = output / "student-card-job.pdf"
    original = b"%PDF-original\n%%EOF"
    replacement = b"%PDF-replacement\n%%EOF"
    rendered.write_bytes(original)
    client = _MemoryS3()

    def client_factory(_service, **kwargs):
        assert kwargs["config"].connect_timeout == 3.0
        assert kwargs["config"].read_timeout == 10.0
        assert kwargs["config"].retries["total_max_attempts"] == 2
        rendered.write_bytes(replacement)
        return client

    with patch("boto3.client", side_effect=client_factory):
        published = publish_document(_settings(output), rendered)

    assert client.last_body == original
    assert client.put_calls[0]["IfNoneMatch"] == "*"
    assert client.put_calls[0]["Key"] == "private/documents/student-card-job.pdf"
    assert client.get_calls == [
        {
            "Bucket": "tryscode",
            "Key": "private/documents/student-card-job.pdf",
            "VersionId": "v1",
        }
    ]
    assert published.size_bytes == len(original)
    assert published.sha256 == hashlib.sha256(original).hexdigest()
    assert published.storage_version == "v1"


def test_s3_existing_identical_object_is_idempotently_reused(tmp_path):
    output = tmp_path / "documents"
    output.mkdir()
    rendered = output / "review-job.pdf"
    body = b"%PDF-review\n%%EOF"
    rendered.write_bytes(body)
    client = _MemoryS3(stored=body, version="existing-v1")
    client.put_error = _precondition_failed()

    with patch("boto3.client", return_value=client):
        published = publish_document(_settings(output), rendered)

    assert published.storage_version == "existing-v1"
    assert published.sha256 == hashlib.sha256(body).hexdigest()
    assert client.get_calls[0].get("VersionId") is None


def test_s3_existing_different_object_fails_closed(tmp_path):
    output = tmp_path / "documents"
    output.mkdir()
    rendered = output / "review-job.pdf"
    rendered.write_bytes(b"%PDF-expected\n%%EOF")
    client = _MemoryS3(stored=b"%PDF-different\n%%EOF")
    client.put_error = _precondition_failed()

    with (
        patch("boto3.client", return_value=client),
        pytest.raises(RuntimeError, match="collision"),
    ):
        publish_document(_settings(output), rendered)


def test_s3_version_mismatch_fails_closed(tmp_path):
    output = tmp_path / "documents"
    output.mkdir()
    rendered = output / "certificate-job.pdf"
    rendered.write_bytes(b"%PDF-certificate\n%%EOF")
    client = _MemoryS3(version="uploaded-v1")

    def get_other_version(**kwargs):
        client.get_calls.append(kwargs)
        return {"Body": _StoredBody(client.stored), "VersionId": "other-v2"}

    client.get_object = get_other_version
    with (
        patch("boto3.client", return_value=client),
        pytest.raises(RuntimeError, match="verification failed"),
    ):
        publish_document(_settings(output), rendered)


def test_s3_publish_refuses_a_symlinked_rendered_path(tmp_path):
    output = tmp_path / "documents"
    output.mkdir()
    target = output / "actual.pdf"
    target.write_bytes(b"%PDF-target\n%%EOF")
    rendered = output / "student-card-job.pdf"
    os.symlink(target, rendered)

    with pytest.raises(RuntimeError, match="cannot be verified"):
        publish_document(_settings(output), rendered)
