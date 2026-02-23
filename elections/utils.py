# elections/utils.py
import hashlib
import json
from django.http import HttpRequest
from .models import VoteRecord

def compute_record_hash(payload_json: str, previous_hash: str | None) -> str:
    hasher = hashlib.sha256()
    hasher.update(payload_json.encode("utf-8"))
    hasher.update((previous_hash or "").encode("utf-8"))
    return hasher.hexdigest()

def request_ip(request: HttpRequest) -> str | None:
    # basic IP extraction (consider more robust proxies in production)
    x_forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")
