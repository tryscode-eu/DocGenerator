"""Fail-closed HTTP primitives for credentialed internal callbacks."""

from __future__ import annotations

import math
import re
from numbers import Real
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_INTERNAL_SERVICE_TOKEN_LENGTH = 4096
_SAFE_PATH = re.compile(r"/[A-Za-z0-9._~/-]+\Z")


class RedirectRefusedError(RuntimeError):
    """Opaque redirect failure that cannot expose a target or a credential."""


class RejectRedirects(HTTPRedirectHandler):
    """Close and reject every redirect status urllib could otherwise follow."""

    @staticmethod
    def _refuse(request, response, code, message, headers):
        del request, code, message, headers
        try:
            response.close()
        except Exception:
            # The redirect remains rejected even if an unusual response object
            # cannot be closed cleanly.
            pass
        raise RedirectRefusedError("internal service redirect refused")

    http_error_301 = _refuse
    http_error_302 = _refuse
    http_error_303 = _refuse
    http_error_307 = _refuse
    http_error_308 = _refuse


# Internal callbacks must not inherit workstation/container proxy variables: a
# proxy would be another place where the service credential could be exposed.
_NO_REDIRECT_OPENER = build_opener(ProxyHandler({}), RejectRedirects())


def validated_callback_url(value: str) -> str:
    """Accept one normalized HTTP(S) URL with an unambiguous absolute path."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
        or "\\" in value
        or "?" in value
        or "#" in value
    ):
        raise ValueError("invalid internal callback URL")

    try:
        parsed = urlsplit(value)
        # Accessing these properties also rejects malformed brackets and ports.
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        raise ValueError("invalid internal callback URL") from None

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "%" in parsed.netloc
        or (parsed.scheme == "http" and hostname.lower() not in {"localhost", "127.0.0.1", "::1"})
    ):
        raise ValueError("invalid internal callback URL")

    path = parsed.path
    if (
        not path
        or not _SAFE_PATH.fullmatch(path)
        or "%" in path
        or "//" in path
        or path.endswith("/")
        or any(segment in {"", ".", ".."} for segment in path.split("/")[1:])
    ):
        raise ValueError("invalid internal callback URL")
    return value


def validated_timeout(value: Real) -> float:
    """Reject invalid/unbounded timeouts instead of relying on socket defaults."""

    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
        or not MIN_TIMEOUT_SECONDS <= float(value) <= MAX_TIMEOUT_SECONDS
    ):
        raise ValueError("invalid internal callback timeout")
    return float(value)


def validated_service_token(value: str) -> str:
    """Accept one opaque header-safe service credential."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_INTERNAL_SERVICE_TOKEN_LENGTH
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError("invalid internal service credential")
    return value


def open_no_redirect(request: Request, *, timeout: Real):
    """Open one validated request without redirects or environment proxies."""

    if not isinstance(request, Request):
        raise TypeError("request must be an urllib Request")
    validated_callback_url(request.full_url)
    return _NO_REDIRECT_OPENER.open(request, timeout=validated_timeout(timeout))
