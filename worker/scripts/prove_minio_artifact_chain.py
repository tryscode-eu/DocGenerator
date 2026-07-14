#!/usr/bin/env python3
"""Opt-in, destructive-only-to-its-bucket MinIO artifact-chain proof.

The caller must provide an isolated MinIO endpoint and a uniquely named proof
bucket.  This program imports the real worker and Harmony implementations; it
does not replace S3 with an in-memory fake.  It deletes every object version
and the bucket before returning.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import boto3
import botocore
import pypdf
from botocore.config import Config
from pypdf import PdfWriter

from doc_worker.storage import publish_document


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _assert_isolated_configuration(
    *,
    endpoint: str,
    bucket: str,
    access_key: str,
    prefix: str,
) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {
        "minio-proof",
        "127.0.0.1",
        "localhost",
    }:
        raise RuntimeError("proof refuses a non-local or TLS S3 endpoint")
    if not bucket.startswith("tryscode-artifact-proof-"):
        raise RuntimeError("proof bucket does not have the required disposable prefix")
    if not access_key.startswith("minio-proof-"):
        raise RuntimeError("proof refuses credentials not reserved for the disposable MinIO")
    if not prefix.startswith("proof-"):
        raise RuntimeError("proof physical prefix does not have the disposable prefix")


def _pdf_bytes(label: str, *, two_pages: bool = False) -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    if two_pages:
        writer.add_blank_page(width=210, height=210)
    writer.add_metadata({"/Title": label})
    writer.write(output)
    return output.getvalue()


def _body_is_closed(body) -> bool:
    if bool(getattr(body, "closed", False)):
        return True
    raw_stream = getattr(body, "_raw_stream", None)
    return bool(getattr(raw_stream, "closed", False))


class _CapturingClient:
    """Transparent client wrapper that records real MinIO GET bodies."""

    def __init__(self, client):
        self._client = client
        self.put_calls = []
        self.get_bodies = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        return self._client.put_object(**kwargs)

    def get_object(self, **kwargs):
        response = self._client.get_object(**kwargs)
        self.get_bodies.append(response["Body"])
        return response

    def __getattr__(self, name):
        return getattr(self._client, name)


class _CapturingBackend:
    """Transparent Harmony backend wrapper that retains reader handles."""

    def __init__(self, backend):
        self._backend = backend
        self.handles = []

    def open_reader(self, key, *, version_id=None):
        handle = self._backend.open_reader(key, version_id=version_id)
        self.handles.append(handle)
        return handle


def _expect(exception_type, operation, *, message: str | None = None):
    try:
        operation()
    except exception_type as exc:
        if message is not None and message not in str(exc):
            raise AssertionError(f"expected {message!r} in {type(exc).__name__}: {exc}") from exc
        return exc
    raise AssertionError(f"expected {exception_type.__name__}")


def _wait_for_minio(client) -> None:
    last_error = None
    for _attempt in range(60):
        try:
            client.list_buckets()
            return
        except Exception as exc:  # endpoint is still starting
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError("isolated MinIO did not become ready") from last_error


def _delete_versioned_bucket(client, bucket: str) -> None:
    while True:
        response = client.list_object_versions(Bucket=bucket)
        identifiers = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for collection in ("Versions", "DeleteMarkers")
            for item in response.get(collection, [])
        ]
        if not identifiers:
            break
        delete_response = client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": identifiers, "Quiet": True},
        )
        if delete_response.get("Errors"):
            raise RuntimeError(f"MinIO cleanup failed: {delete_response['Errors']}")
    client.delete_bucket(Bucket=bucket)
    remaining = {entry["Name"] for entry in client.list_buckets().get("Buckets", [])}
    if bucket in remaining:
        raise RuntimeError("proof bucket still exists after cleanup")


def main() -> None:
    endpoint = _required_environment("S3_ENDPOINT_URL")
    bucket = _required_environment("S3_BUCKET")
    access_key = _required_environment("S3_ACCESS_KEY")
    secret_key = _required_environment("S3_SECRET_KEY")
    region = os.environ.get("S3_REGION", "us-east-1")
    physical_prefix = _required_environment("S3_PREFIX").strip("/")
    _assert_isolated_configuration(
        endpoint=endpoint,
        bucket=bucket,
        access_key=access_key,
        prefix=physical_prefix,
    )

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(
            connect_timeout=1,
            read_timeout=3,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )
    bucket_created = False
    proof_succeeded = False
    try:
        _wait_for_minio(client)
        client.create_bucket(Bucket=bucket)
        bucket_created = True
        client.put_bucket_versioning(
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
        versioning = client.get_bucket_versioning(Bucket=bucket)
        assert versioning.get("Status") == "Enabled"

        original = _pdf_bytes("TrysCode isolated artifact proof")
        altered = _pdf_bytes("TrysCode altered artifact proof", two_pages=True)
        assert original != altered
        logical_filename = "review-proof-job.pdf"
        logical_key = f"documents/{logical_filename}"
        physical_key = f"{physical_prefix}/{logical_key}"

        worker_settings = SimpleNamespace(
            document_output_dir=None,
            document_storage_mode="s3",
            document_storage_prefix="documents",
            s3_prefix=physical_prefix,
            s3_endpoint_url=endpoint,
            s3_bucket=bucket,
            s3_access_key=access_key,
            s3_secret_key=secret_key,
            s3_region=region,
            s3_connect_timeout_seconds=1.0,
            s3_read_timeout_seconds=3.0,
            s3_max_attempts=1,
        )

        capturing_client = _CapturingClient(client)
        with tempfile.TemporaryDirectory(prefix="tryscode-minio-proof-") as output:
            output_dir = Path(output)
            rendered = output_dir / logical_filename
            rendered.write_bytes(original)
            worker_settings.document_output_dir = output_dir

            with patch("boto3.client", return_value=capturing_client):
                first = publish_document(worker_settings, rendered)
                repeated = publish_document(worker_settings, rendered)
                rendered.write_bytes(altered)
                _expect(
                    RuntimeError,
                    lambda: publish_document(worker_settings, rendered),
                    message="collision",
                )

        assert first.storage_key == logical_key
        assert first.size_bytes == len(original)
        assert first.sha256 == hashlib.sha256(original).hexdigest()
        assert first.storage_version
        assert repeated == first
        assert len(capturing_client.put_calls) == 3
        assert all(
            call.get("IfNoneMatch") == "*"
            and call.get("Bucket") == bucket
            and call.get("Key") == physical_key
            for call in capturing_client.put_calls
        )
        assert len(capturing_client.get_bodies) == 3
        assert all(_body_is_closed(body) for body in capturing_client.get_bodies)

        listed = client.list_objects_v2(Bucket=bucket).get("Contents", [])
        assert [item["Key"] for item in listed] == [physical_key]
        head = client.head_object(Bucket=bucket, Key=physical_key)
        assert head.get("VersionId") == first.storage_version
        initial_versions = [
            item
            for item in client.list_object_versions(
                Bucket=bucket,
                Prefix=physical_key,
            ).get("Versions", [])
            if item["Key"] == physical_key
        ]
        assert len(initial_versions) == 1
        assert initial_versions[0]["VersionId"] == first.storage_version

        # Import Harmony only after its isolated S3 environment has been set.
        import app.models.__all_models  # noqa: F401 - register relationship targets
        from app.models.job import Job, JobStatus
        from app.services.document_artifacts import (
            DOCUMENT_CALLBACK_VERSION,
            DOCUMENT_RENDER_CONTRACT_VERSION,
            assert_same_document_artifact,
            validate_document_callback_claims,
        )
        from app.services.storage import (
            StorageService,
            StoredDocumentIntegrityError,
        )
        from app.services.storage_backend import S3Backend

        job = Job(
            id=41,
            type="review_document",
            queue="document_tasks",
            entity_kind="review",
            entity_key="proof-review",
            dedupe_key="review:proof-review:document",
            status=JobStatus.done,
            progress=100,
            payload={
                "contract_version": DOCUMENT_RENDER_CONTRACT_VERSION,
                "_expected_result_path": logical_key,
            },
        )
        claims = validate_document_callback_claims(
            job=job,
            status=JobStatus.done,
            progress=100,
            callback_version=DOCUMENT_CALLBACK_VERSION,
            result_path=first.storage_key,
            result_size_bytes=first.size_bytes,
            result_sha256=first.sha256,
            result_storage_version=first.storage_version,
            error_message=None,
            report_payload=None,
        )
        assert claims is not None

        backend = _CapturingBackend(S3Backend())

        def stage(
            *,
            size=claims.size_bytes,
            digest=claims.sha256,
            version=claims.storage_version,
        ):
            with patch("app.services.storage.get_backend", return_value=backend):
                return StorageService().stage_verified_pdf(
                    claims.storage_key,
                    expected_size_bytes=size,
                    expected_sha256=digest,
                    storage_version=version,
                )

        exact = stage()
        assert exact.storage_version == first.storage_version
        assert b"".join(exact.iter_chunks()) == original
        assert _body_is_closed(backend.handles[-1].reader)

        _expect(
            StoredDocumentIntegrityError,
            lambda: stage(size=claims.size_bytes + 1),
            message="size mismatch",
        )
        assert _body_is_closed(backend.handles[-1].reader)
        _expect(
            StoredDocumentIntegrityError,
            lambda: stage(digest="0" * 64),
            message="digest mismatch",
        )
        assert _body_is_closed(backend.handles[-1].reader)

        # Simulate an out-of-band storage alteration after the immutable worker
        # publication. Version pinning still reads v1; unpinned/current reads and
        # a callback that attempts to replace the identity are rejected.
        altered_put = client.put_object(
            Bucket=bucket,
            Key=physical_key,
            Body=altered,
            ContentType="application/pdf",
        )
        altered_version = altered_put.get("VersionId")
        assert altered_version and altered_version != first.storage_version
        assert (
            client.head_object(Bucket=bucket, Key=physical_key).get("VersionId") == altered_version
        )

        pinned = stage()
        assert pinned.storage_version == first.storage_version
        assert b"".join(pinned.iter_chunks()) == original
        assert _body_is_closed(backend.handles[-1].reader)

        _expect(
            StoredDocumentIntegrityError,
            lambda: stage(version=None),
            message="size mismatch",
        )
        assert _body_is_closed(backend.handles[-1].reader)
        _expect(
            StoredDocumentIntegrityError,
            lambda: stage(version=altered_version),
            message="size mismatch",
        )
        assert _body_is_closed(backend.handles[-1].reader)

        persisted_artifact = SimpleNamespace(
            storage_key=claims.storage_key,
            size_bytes=claims.size_bytes,
            sha256=claims.sha256,
            storage_version=claims.storage_version,
        )
        assert_same_document_artifact(persisted_artifact, claims)
        altered_claims = validate_document_callback_claims(
            job=job,
            status=JobStatus.done,
            progress=100,
            callback_version=DOCUMENT_CALLBACK_VERSION,
            result_path=logical_key,
            result_size_bytes=len(altered),
            result_sha256=hashlib.sha256(altered).hexdigest(),
            result_storage_version=altered_version,
            error_message=None,
            report_payload=None,
        )
        assert altered_claims is not None
        _expect(
            ValueError,
            lambda: assert_same_document_artifact(
                persisted_artifact,
                altered_claims,
            ),
            message="changed its artifact identity",
        )

        assert all(_body_is_closed(handle.reader) for handle in backend.handles)
        versions_after_alteration = [
            item
            for item in client.list_object_versions(
                Bucket=bucket,
                Prefix=physical_key,
            ).get("Versions", [])
            if item["Key"] == physical_key
        ]
        assert {item["VersionId"] for item in versions_after_alteration} == {
            first.storage_version,
            altered_version,
        }

        proof_succeeded = True
        print(
            json.dumps(
                {
                    "proof": "tryscode.minio-document-artifact.v1",
                    "boto3": boto3.__version__,
                    "botocore": botocore.__version__,
                    "pypdf": pypdf.__version__,
                    "bucket_versioning": versioning["Status"],
                    "logical_key": logical_key,
                    "physical_key": physical_key,
                    "initial_version_id": first.storage_version,
                    "altered_version_id": altered_version,
                    "assertions": {
                        "worker_if_none_match_create_once": True,
                        "worker_identical_retry_reuses_identity": True,
                        "worker_different_collision_rejected": True,
                        "worker_get_bodies_closed": True,
                        "physical_prefix_exact": True,
                        "harmony_exact_claims_accepted": True,
                        "harmony_size_digest_mismatch_rejected": True,
                        "harmony_version_pin_survives_current_tamper": True,
                        "harmony_current_tamper_rejected": True,
                        "harmony_changed_callback_identity_rejected": True,
                        "harmony_get_bodies_closed": True,
                    },
                },
                sort_keys=True,
            )
        )
    finally:
        cleanup_succeeded = False
        try:
            if bucket_created:
                _delete_versioned_bucket(client, bucket)
            cleanup_succeeded = True
        finally:
            client.close()
            print(
                json.dumps(
                    {
                        "cleanup": {
                            "bucket_deleted": cleanup_succeeded,
                            "proof_had_succeeded": proof_succeeded,
                        }
                    },
                    sort_keys=True,
                )
            )


if __name__ == "__main__":
    main()
