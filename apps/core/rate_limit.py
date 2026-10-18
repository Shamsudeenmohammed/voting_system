"""Cache-backed sliding-window rate limiting.

Deliberately simple: no external dependency, works with LocMem cache (single
process) or Redis (multi-process). In distributed production the Redis cache
is mandatory — see docs/deployment-online.md.
"""
from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"rate limited, retry in {retry_after}s")


def _key(bucket: str, ident: str) -> str:
    return f"rl:{bucket}:{ident}"


def check_rate(bucket: str, ident: str) -> int:
    """Return remaining allowance; raise RateLimited when exhausted."""
    limit, window = settings.RATE_LIMITS[bucket]
    key = _key(bucket, ident)
    try:
        hits = cache.get(key)
    except Exception:  # cache outage must not lock everyone out
        return limit
    if hits is None:
        hits = 0
    if hits >= limit:
        raise RateLimited(window)
    try:
        cache.set(key, hits + 1, timeout=window)
    except Exception:
        pass
    return limit - hits - 1


def rate_limit(bucket: str, identity_getter):
    """Decorator. ``identity_getter(request)`` returns a stable client id."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            ident = identity_getter(request) or "anon"
            try:
                check_rate(bucket, f"{bucket}:{ident}")
            except RateLimited as exc:
                resp = HttpResponse(
                    "Too many requests. Please wait and try again.", status=429
                )
                resp["Retry-After"] = str(exc.retry_after)
                return resp
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def client_ip(request) -> str:
    """Client IP for *abuse detection only*.

    Never stored alongside ballot selections (see apps.ballots.models) — only
    used transiently for rate limiting and authorization-side logging.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")
