"""Cryptographic key handling for result/audit signatures.

* Keys are **never** stored in source control.  Production supplies
  ``RESULT_SIGNING_KEY`` (PEM) through the environment/secret manager; local
  mode auto-generates a key file under ``secrets/`` (git-ignored).
* The algorithm is Ed25519 when the ``cryptography`` package is available
  (it is pinned in requirements.txt); HMAC-SHA256 is the deterministic
  fallback that only the server can verify (documented limitation).
"""

from __future__ import annotations

import base64
import hmac
import hashlib
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = __import__("logging").getLogger("votesys.audit")

_ed25519 = None
try:  # pragma: no cover - exercised implicitly
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    _ed25519 = True
except ImportError:  # pragma: no cover
    Ed25519PrivateKey = Ed25519PublicKey = serialization = None  # type: ignore


def _read_key_material() -> str:
    if settings.RESULT_SIGNING_KEY:
        return settings.RESULT_SIGNING_KEY
    path: Path = settings.RESULT_SIGNING_KEY_FILE
    if path.exists():
        return path.read_text(encoding="utf-8")
    if settings.DEBUG:
        if _ed25519 is None:
            raise ImproperlyConfigured("cryptography is required to generate signing keys.")
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pem, encoding="utf-8")
        try:  # best effort POSIX hardening
            path.chmod(0o600)
        except OSError:  # pragma: no cover - windows
            pass
        return pem
    raise ImproperlyConfigured(
        "RESULT_SIGNING_KEY (PEM) is not configured and no key file exists at "
        f"{path}."
    )


def _private_key():
    pem = _read_key_material()
    if _ed25519 is None:  # pragma: no cover
        return None
    if "BEGIN" not in pem:  # raw secret -> HMAC mode
        return None
    try:
        return serialization.load_pem_private_key(pem.encode(), password=None)
    except Exception as exc:  # pragma: no cover
        raise ImproperlyConfigured(f"RESULT_SIGNING_KEY is not a valid PEM: {exc}") from exc


def algorithm() -> str:
    return "ed25519" if _private_key() is not None else "hmac-sha256"


def public_key_pem() -> str:
    key = _private_key()
    if key is None:
        return ""
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def sign(message: bytes) -> str:
    """Return a base64 signature over ``message``."""
    key = _private_key()
    if key is not None:
        return base64.b64encode(key.sign(message)).decode()
    secret = _read_key_material().encode()
    return base64.b64encode(hmac.new(secret, message, hashlib.sha256).digest()).decode()


def verify(message: bytes, signature: str, public_pem: str = "") -> bool:
    """Verify ``signature`` over ``message``.

    ``public_pem`` is required for Ed25519 signatures produced by an external
    checkpoint; for locally produced HMAC signatures the server secret is used.
    """
    try:
        raw = base64.b64decode(signature)
    except Exception:
        return False
    if public_pem:
        if _ed25519 is None:  # pragma: no cover
            return False
        try:
            pub = serialization.load_pem_public_key(public_pem.encode())
            pub.verify(raw, message)
            return True
        except Exception:
            return False
    key = _private_key()
    if key is not None:
        try:
            key.public_key().verify(raw, message)
            return True
        except Exception:
            return False
    secret = _read_key_material().encode()
    expected = hmac.new(secret, message, hashlib.sha256).digest()
    return hmac.compare_digest(expected, raw)
