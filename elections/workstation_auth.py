"""Workstation (managed device) authentication helpers."""

from __future__ import annotations

from django.utils import timezone

SESSION_KEY = "workstation_auth"


def get_workstation(request):
    from elections.models import Workstation

    data = request.session.get(SESSION_KEY) if hasattr(request, "session") else None
    if data:
        ws = Workstation.objects.filter(pk=data.get("id"), status=Workstation.STATUS_ACTIVE).first()
        if ws is not None:
            ws.last_seen_at = timezone.now()
            ws.save(update_fields=["last_seen_at", "updated_at"])
            return ws
        _clear_session(request)

    code = request.META.get("HTTP_X_WORKSTATION_CODE", "").strip()
    secret = request.META.get("HTTP_X_WORKSTATION_CREDENTIAL", "").strip()
    if code and secret:
        ws = Workstation.objects.filter(code=code).first()
        if ws is not None and ws.status == Workstation.STATUS_ACTIVE and ws.check_credential(secret):
            ws.last_seen_at = timezone.now()
            ws.save(update_fields=["last_seen_at", "updated_at"])
            return ws
    return None


def authenticate_workstation(code, secret):
    from elections.models import Workstation

    code = (code or "").strip()
    secret = (secret or "").strip()
    if not code or not secret:
        return None
    ws = Workstation.objects.filter(code=code).first()
    if ws is None or ws.status != Workstation.STATUS_ACTIVE:
        return None
    if not ws.check_credential(secret):
        return None
    return ws


def login_workstation(request, workstation):
    request.session.cycle_key()
    request.session[SESSION_KEY] = {"id": str(workstation.pk), "auth_at": int(timezone.now().timestamp())}
    request.session.modified = True


def logout_workstation(request):
    _clear_session(request)


def _clear_session(request):
    if hasattr(request, "session"):
        request.session.pop(SESSION_KEY, None)
        request.session.modified = True


def require_workstation(request, purpose=None):
    from django.http import HttpResponseForbidden, HttpResponseRedirect
    from django.urls import reverse

    ws = get_workstation(request)
    if ws is None:
        try:
            url = reverse("workstation-login")
        except Exception:
            url = "/workstation/login/"
        return None, HttpResponseRedirect(url)
    if purpose is not None and ws.purpose != purpose:
        return None, HttpResponseForbidden("This workstation is not permitted to perform that action.")
    return ws, None