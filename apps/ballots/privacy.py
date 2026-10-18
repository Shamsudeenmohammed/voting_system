"""Privacy helpers: time quantization and payload sealing.

Selections are sealed with Fernet (AES-128-CBC + HMAC under the hood — an
established construction, not an improvised cipher). The key comes from the
environment and never touches the database. This protects data at rest
(DB dumps, backups, replica theft); it does NOT provide end-to-end
verifiability — see docs/ballot-secrecy.md §Limitations.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is None:
        from cryptography.fernet import Fernet

        key = settings.BALLOT_ENCRYPTION_KEY
        if not key:
            raise RuntimeError(
                "BALLOT_ENCRYPTION_KEY is not configured. "
                "Generate one: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def quantize_time(dt, window_minutes: int | None = None):
    """Round timestamp down to the privacy window (default from settings).

    Keeps identity-side consumption times from matching selection-side
    timestamps to the second.
    """
    from datetime import datetime

    window = window_minutes or int(getattr(settings, "BALLOT_TIME_WINDOW_MINUTES", 5))
    if dt is None:
        return None
    floored = dt.replace(second=0, microsecond=0)
    minutes = (floored.hour * 60 + floored.minute) // window * window
    return floored.replace(hour=minutes // 60, minute=minutes % 60)


def seal_selections(selections: dict) -> tuple[bytes, str]:
    """Return (ciphertext, sha256_hex) for a canonical selections dict."""
    canonical = json.dumps(selections, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return _get_fernet().encrypt(canonical.encode("utf-8")), digest


def unseal_selections(sealed: bytes) -> dict:
    """Decrypt sealed selections. Used ONLY by the tally engine."""
    plaintext = _get_fernet().decrypt(bytes(sealed))
    return json.loads(plaintext.decode("utf-8"))


def new_token() -> tuple[str, str]:
    """Return (raw_token, sha256_hash). Raw token shown once, never stored."""
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_receipt() -> tuple[str, str]:
    raw = secrets.token_urlsafe(24)
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_receipt(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def expires_at(ttl: int | None = None):
    ttl = ttl or settings.BALLOT_TOKEN_TTL
    return timezone.now() + timedelta(seconds=ttl)
