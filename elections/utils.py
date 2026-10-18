"""Small utilities shared by presentation and audit layers."""
from __future__ import annotations

import secrets


def request_ip(request) -> str:
    """Best-effort client IP (never used as an identity anchor for ballots)."""
    if request is None:
        return ""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or ""


def new_credential_token() -> str:
    """Return a fresh, unguessable ballot-authorization token.

    Only a peppered hash is ever persisted (see ballot.crypto.hash_token).
    """
    return secrets.token_urlsafe(32)


def new_admin_token() -> str:
    """Opaque token for polling-station administrator cards."""
    return secrets.token_urlsafe(24)
