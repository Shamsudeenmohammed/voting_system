"""Cryptographic helpers for the ballot engine.

Threat model notes (see docs/BALLOT_SECRECY.md):

* ``payload`` is encrypted at rest with Fernet (AES-128-CBC + HMAC) using a
  server-side key that never enters the repository.  Encryption at rest is a
  *defence-in-depth* measure - it does **not** by itself provide anonymity;
  the separation of the authorization record from the ballot record does.
* Receipt codes and submission ids are generated with ``secrets`` (CSPRNG).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from functools import lru_cache

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

_fernet_cls = None
try:  # pragma: no cover - exercised implicitly
    from cryptography.fernet import Fernet

    _fernet_cls = Fernet
except ImportError:  # pragma: no cover
    Fernet = None  # type: ignore


def _pepper() -> bytes:
    return (settings.VOTER_TOKEN_PEPPER or settings.SECRET_KEY).encode()


def hash_token(token: str) -> str:
    """Peppered HMAC-SHA256 of a voter access/ballot token.

    Lookup is performed by hashing the presented token, so the database
    never stores a reversible credential.
    """
    return hmac.new(_pepper(), token.encode(), hashlib.sha256).hexdigest()


@lru_cache(maxsize=4)
def get_fernet():
    if _fernet_cls is None:  # pragma: no cover
        raise ImproperlyConfigured(
            "The 'cryptography' package is required for ballot encryption."
        )
    configured = (settings.BALLOT_ENCRYPTION_KEY or "").strip()
    if configured:
        try:
            return _fernet_cls(configured.encode())
        except Exception as exc:
            raise ImproperlyConfigured(
                "BALLOT_ENCRYPTION_KEY must be a urlsafe-base64 encoded 32-byte key "
                "(generate with: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\")."
            ) from exc
    path = settings.BALLOT_KEY_FILE
    if path.exists():
        key = path.read_bytes().strip()
        try:
            return _fernet_cls(key)
        except Exception as exc:
            raise ImproperlyConfigured(f"Invalid ballot key file {path}: {exc}") from exc
    if settings.DEBUG:
        key = _fernet_cls.generate_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key)
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover - non-POSIX
            pass
        return _fernet_cls(key)
    raise ImproperlyConfigured(
        "No ballot encryption key configured. Set BALLOT_ENCRYPTION_KEY or "
        f"provide an existing key file at {path}."
    )


def encrypt_payload(data: dict, fernet=None) -> str:
    fernet = fernet or get_fernet()
    material = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return fernet.encrypt(material.encode()).decode()


def decrypt_payload(token: str, fernet=None) -> dict:
    fernet = fernet or get_fernet()
    raw = fernet.decrypt(token.encode())
    return json.loads(raw)


def new_access_token() -> str:
    """24-char URL-safe token handed to a voter (delivered out of band)."""
    return secrets.token_urlsafe(18)


def new_receipt_code() -> str:
    """24-hex-char receipt shown to the voter after acceptance."""
    return secrets.token_hex(12)


def new_submission_id():
    import uuid

    return uuid.uuid4()


def ballot_hash(
    *, election_id: str, submission_id, receipt: str, payload: str, config_hash: str
) -> str:
    """Deterministic identifier of an accepted ballot (for manifests)."""
    material = json.dumps(
        {
            "election": election_id,
            "submission": str(submission_id),
            "receipt": receipt,
            "payload": payload,
            "config": config_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()
