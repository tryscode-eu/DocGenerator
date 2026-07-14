"""Publish generated PDFs without ever replacing a different artifact."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .config import SAFE_DOCUMENT_STORAGE_PREFIX

MAX_DOCUMENT_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PublishedDocument:
    storage_key: str
    size_bytes: int
    sha256: str
    storage_version: str | None = None


def _validated_version(value) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise RuntimeError("document storage returned an invalid version")
    return value


def _rendered_snapshot(path: Path) -> tuple[bytes, int, str]:
    """Read and hash one bounded regular file through the same descriptor."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("rendered document is not a regular file")
        digest = hashlib.sha256()
        snapshot = bytearray()
        size_bytes = 0
        with os.fdopen(descriptor, "rb", closefd=True) as document:
            descriptor = None
            signature = document.read(5)
            if signature != b"%PDF-":
                raise RuntimeError("rendered document has an invalid signature")
            digest.update(signature)
            snapshot.extend(signature)
            size_bytes = len(signature)
            while True:
                chunk = document.read(64 * 1024)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > MAX_DOCUMENT_BYTES:
                    raise RuntimeError("rendered document exceeds the size limit")
                digest.update(chunk)
                snapshot.extend(chunk)
        return bytes(snapshot), size_bytes, digest.hexdigest()
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("rendered document cannot be verified") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _s3_object_identity(
    client,
    *,
    bucket: str,
    key: str,
    version_id: str | None,
) -> tuple[int, str, str | None]:
    request = {"Bucket": bucket, "Key": key}
    if version_id is not None:
        request["VersionId"] = version_id
    try:
        response = client.get_object(**request)
        body = response["Body"]
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            signature = body.read(5)
            if signature != b"%PDF-":
                raise RuntimeError("stored document has an invalid signature")
            digest.update(signature)
            size_bytes = len(signature)
            while True:
                chunk = body.read(64 * 1024)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > MAX_DOCUMENT_BYTES:
                    raise RuntimeError("stored document exceeds the size limit")
                digest.update(chunk)
        finally:
            body.close()
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("document storage verification failed") from None
    return size_bytes, digest.hexdigest(), _validated_version(response.get("VersionId"))


def publish_document(settings, path: Path) -> PublishedDocument:
    """Publish once, then return the exact identity of the stored PDF bytes."""

    output_dir = Path(settings.document_output_dir).resolve()
    rendered = Path(path).expanduser()
    if not rendered.is_absolute():
        rendered = Path.cwd() / rendered
    try:
        rendered_parent = rendered.parent.resolve(strict=True)
    except OSError:
        raise RuntimeError("unsafe rendered document path") from None
    if rendered_parent != output_dir or rendered.suffix.lower() != ".pdf":
        raise RuntimeError("unsafe rendered document path")
    prefix = settings.document_storage_prefix.strip("/")
    if not SAFE_DOCUMENT_STORAGE_PREFIX.fullmatch(prefix):
        raise RuntimeError("invalid document storage prefix")
    key = f"{prefix}/{rendered.name}"
    snapshot, size_bytes, sha256 = _rendered_snapshot(rendered)
    if settings.document_storage_mode.lower() == "local":
        # Renderer installation is create-once and Harmony mounts this same root.
        return PublishedDocument(key, size_bytes, sha256)

    object_prefix = settings.s3_prefix.strip("/")
    physical_key = f"{object_prefix}/{key}" if object_prefix else key
    client = None
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region or None,
            config=Config(
                connect_timeout=settings.s3_connect_timeout_seconds,
                read_timeout=settings.s3_read_timeout_seconds,
                retries={
                    "total_max_attempts": settings.s3_max_attempts,
                    "mode": "standard",
                },
            ),
        )
        response = client.put_object(
            Bucket=settings.s3_bucket,
            Key=physical_key,
            Body=snapshot,
            ContentType="application/pdf",
            IfNoneMatch="*",
        )
    except Exception as exc:
        error_response = getattr(exc, "response", None)
        error = error_response.get("Error", {}) if isinstance(error_response, dict) else {}
        status = (
            error_response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if isinstance(error_response, dict)
            else None
        )
        if client is not None and (
            error.get("Code") in {"PreconditionFailed", "412"} or status == 412
        ):
            existing_size, existing_sha256, existing_version = _s3_object_identity(
                client,
                bucket=settings.s3_bucket,
                key=physical_key,
                version_id=None,
            )
            if (existing_size, existing_sha256) != (size_bytes, sha256):
                raise RuntimeError("document storage key collision") from None
            return PublishedDocument(
                key,
                size_bytes,
                sha256,
                existing_version,
            )
        raise RuntimeError("document storage upload failed") from None

    uploaded_version = _validated_version(response.get("VersionId"))
    stored_size, stored_sha256, observed_version = _s3_object_identity(
        client,
        bucket=settings.s3_bucket,
        key=physical_key,
        version_id=uploaded_version,
    )
    if (stored_size, stored_sha256) != (size_bytes, sha256):
        raise RuntimeError("document storage verification failed")
    if uploaded_version != observed_version and (
        uploaded_version is not None or observed_version is not None
    ):
        raise RuntimeError("document storage verification failed")
    return PublishedDocument(key, size_bytes, sha256, observed_version)
