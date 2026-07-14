import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .http_client import (
    validated_callback_url,
    validated_service_token,
    validated_timeout,
)

SAFE_DOCUMENT_STORAGE_PREFIX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class Settings(BaseSettings):
    rabbitmq_url: str = ""
    rabbitmq_socket_timeout_seconds: float = Field(default=3.0, gt=0, le=10)
    rabbitmq_stack_timeout_seconds: float = Field(default=5.0, gt=0, le=15)
    rabbitmq_blocked_timeout_seconds: float = Field(default=3.0, gt=0, le=10)
    rabbitmq_heartbeat_seconds: int = Field(default=30, ge=5, le=300)
    document_queue: str = "document_tasks"
    document_retry_queue: str = "document_tasks.retry"
    document_archive_queue: str = "document_tasks.archive"
    document_max_retries: int = Field(default=5, ge=0, le=20)
    document_retry_delay_seconds: int = Field(default=30, ge=1, le=86_400)
    document_retry_signing_key: str = ""
    document_retry_max_messages: int = Field(default=10_000, ge=1, le=1_000_000)
    document_retry_max_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=5 * 1024 * 1024,
        le=2_147_483_647,
    )
    document_archive_ttl_ms: int = Field(
        default=7 * 24 * 60 * 60 * 1000,
        ge=60_000,
        le=2_147_483_647,
    )
    document_archive_max_messages: int = Field(
        default=10_000,
        ge=1,
        le=1_000_000,
    )
    document_output_dir: Path = Path("output/documents")
    document_storage_mode: str = "local"
    document_storage_prefix: str = "documents"
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = ""
    s3_prefix: str = ""
    s3_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=10)
    s3_read_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    s3_max_attempts: int = Field(default=2, ge=1, le=4)
    harmony_callback_url: str = ""
    harmony_service_token: str = ""
    document_callback_timeout_seconds: int = Field(default=5, ge=1, le=30)
    debug: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def validate_settings(settings: Settings) -> None:
    if not settings.rabbitmq_url:
        raise RuntimeError("Configuration incomplète : RABBITMQ_URL est requis")
    if settings.rabbitmq_stack_timeout_seconds <= settings.rabbitmq_socket_timeout_seconds:
        raise RuntimeError(
            "RABBITMQ_STACK_TIMEOUT_SECONDS doit dépasser RABBITMQ_SOCKET_TIMEOUT_SECONDS"
        )
    queue_names = (
        settings.document_queue,
        settings.document_retry_queue,
        settings.document_archive_queue,
    )
    if any(
        not isinstance(name, str)
        or not name
        or len(name) > 128
        or any(ord(character) < 33 or ord(character) > 126 for character in name)
        for name in queue_names
    ):
        raise RuntimeError("Les noms de files document sont invalides")
    if len(set(queue_names)) != len(queue_names):
        raise RuntimeError("Les files document, retry et archive doivent être distinctes")
    storage_prefix = settings.document_storage_prefix.strip("/")
    if not SAFE_DOCUMENT_STORAGE_PREFIX.fullmatch(storage_prefix):
        raise RuntimeError("DOCUMENT_STORAGE_PREFIX est invalide")
    if settings.document_storage_mode.lower() not in {"local", "s3"}:
        raise RuntimeError("DOCUMENT_STORAGE_MODE doit être local ou s3")
    if settings.document_storage_mode.lower() == "s3":
        if not all(
            (
                settings.s3_endpoint_url,
                settings.s3_bucket,
                settings.s3_access_key,
                settings.s3_secret_key,
            )
        ):
            raise RuntimeError("La configuration S3 des documents est incomplète")
        s3_prefix = settings.s3_prefix.strip("/")
        if (
            len(s3_prefix) > 512
            or any(segment in {"", ".", ".."} for segment in s3_prefix.split("/"))
            and bool(s3_prefix)
            or any(ord(character) < 33 or ord(character) > 126 for character in s3_prefix)
        ):
            raise RuntimeError("S3_PREFIX est invalide")
    if not settings.harmony_callback_url or not settings.harmony_service_token:
        raise RuntimeError("Le callback Harmony et son token sont requis")
    signing_key = settings.document_retry_signing_key.encode("utf-8", errors="strict")
    if not 32 <= len(signing_key) <= 256 or any(
        character < 0x21 or character > 0x7E for character in signing_key
    ):
        raise RuntimeError("DOCUMENT_RETRY_SIGNING_KEY est invalide")
    if settings.document_retry_signing_key == settings.harmony_service_token:
        raise RuntimeError("DOCUMENT_RETRY_SIGNING_KEY doit être distinct de HARMONY_SERVICE_TOKEN")
    try:
        validated_callback_url(settings.harmony_callback_url)
        validated_service_token(settings.harmony_service_token)
        validated_timeout(settings.document_callback_timeout_seconds)
    except ValueError as exc:
        raise RuntimeError("La configuration du callback Harmony est invalide") from exc
