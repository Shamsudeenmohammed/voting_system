"""Views for workstation registration, authentication and access modes."""

from __future__ import annotations

from types import SimpleNamespace

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.db import IntegrityError
from django.templatetags.static import static
from django.views.decorators.http import require_http_methods, require_POST

from organizations.roles import is_system_admin
from elections.models import Election, PollingStation, Workstation, Voter
from elections.workstation_auth import (
    authenticate_workstation,
    get_workstation,
    login_workstation,
    logout_workstation,
    require_workstation,
)
from ballot.models import BallotAuthorization
from ballot.services import (
    TokenIssuanceError,
    issue_voter_token,
    revoke_voter_token,
)


DEFAULT_VERIFICATION_BACKGROUND = "img/workstation-background.svg"


def _verification_background_url() -> str:
    """Resolve the operator-supplied workstation backdrop to a URL.

    Relative values are treated as static files so they keep working with
    hashed static storage; absolute paths and full URLs are used verbatim
    (useful for images served from ``MEDIA_URL`` or a CDN).
    """
    value = (
        getattr(settings, "VERIFICATION_BACKGROUND_IMAGE", "")
        or DEFAULT_VERIFICATION_BACKGROUND
    ).strip()
    if value.startswith(("http://", "https://", "//", "/", "data:")):
        return value
    return static(value)


def _verification_error(message: str, voter=None) -> SimpleNamespace:
    return SimpleNamespace(
        status="error",
        voter=voter,
        authorization=None,
        token=None,
        message=message,
    )


def _verification_token_state(voter, election) -> dict:
    """Summarise the voter's current token for the verification card.

    ``can_manage`` is true only for an unused token, which is why an official
    can still replace or revoke an expired token but never one backing a cast
    ballot.
    """
    auths = BallotAuthorization.objects.filter(voter=voter, election=election)
    consumed = (
        auths.filter(consumed_at__isnull=False)
        .order_by("-consumed_at")
        .first()
    )
    unused = list(
        auths.filter(consumed_at__isnull=True, revoked_at__isnull=True)
        .order_by("-issued_at")
    )
    active = next((auth for auth in unused if auth.usable), None)
    authorization = active or (
        unused[0] if unused else auths.order_by("-issued_at").first()
    )

    if voter.has_voted or consumed is not None:
        state = "used"
    elif active is not None:
        state = "active"
    elif unused:
        state = "expired"
    elif authorization is not None and authorization.revoked_at:
        state = "revoked"
    else:
        state = "none"

    return {
        "state": state,
        "authorization": authorization,
        "can_manage": state in {"active", "expired"},
    }


def _super_admin_or_403(request):
    if not is_system_admin(request.user):
        raise PermissionDenied("Super Admin access required.")


@login_required
@require_http_methods(["GET", "POST"])
def workstation_manage(request):
    """Super Admin workstation management section."""
    _super_admin_or_403(request)
    from django.contrib import messages

    workstations = Workstation.objects.select_related("election", "polling_station", "created_by").order_by("name")
    elections = Election.objects.order_by("-created_at")[:200]
    stations = PollingStation.objects.select_related("election").order_by("name")[:500]

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "create":
            ws = Workstation(
                name=request.POST.get("name", "").strip(),
                code=request.POST.get("code", "").strip(),
                purpose=request.POST.get("purpose", Workstation.PURPOSE_VOTING),
                election_id=request.POST.get("election") or None,
                polling_station_id=request.POST.get("polling_station") or None,
                status=Workstation.STATUS_ACTIVE,
                created_by=request.user,
            )
            raw_cred = request.POST.get("credential", "").strip()
            if not ws.name or not ws.code:
                messages.error(request, "Name and code are required.")
                return redirect("workstation-manage")
            if not raw_cred:
                messages.error(request, "Device credential is required.")
                return redirect("workstation-manage")
            if Workstation.objects.filter(code=ws.code).exists():
                messages.error(request, f"A workstation with code {ws.code!r} already exists.")
                return redirect("workstation-manage")
            ws.set_credential(raw_cred)
            try:
                ws.save()
            except IntegrityError:
                messages.error(request, f"A workstation with code {ws.code!r} already exists.")
                return redirect("workstation-manage")
            messages.success(request, f"Workstation {ws.code} registered.")
            return redirect("workstation-manage")
        if action in {"activate", "disable", "revoke"}:
            pk = request.POST.get("id")
            ws = get_object_or_404(Workstation, pk=pk)
            if action == "activate":
                ws.activate()
            elif action == "disable":
                ws.disable()
            elif action == "revoke":
                ws.revoke()
            ws.save(update_fields=["status", "updated_at"])
            messages.success(request, f"Workstation {ws.code}: {ws.status}.")
            return redirect("workstation-manage")
        if action == "edit":
            pk = request.POST.get("id")
            ws = get_object_or_404(Workstation, pk=pk)
            ws.name = request.POST.get("name", ws.name).strip() or ws.name
            ws.code = request.POST.get("code", ws.code).strip() or ws.code
            ws.purpose = request.POST.get("purpose", ws.purpose)
            ws.election_id = request.POST.get("election") or None
            ws.polling_station_id = request.POST.get("polling_station") or None
            ws.status = request.POST.get("status", ws.status)
            raw_cred = request.POST.get("credential", "").strip()
            if Workstation.objects.exclude(pk=ws.pk).filter(code=ws.code).exists():
                messages.error(request, f"A workstation with code {ws.code!r} already exists.")
                return redirect("workstation-manage")
            if raw_cred:
                ws.set_credential(raw_cred)
            try:
                ws.save()
            except IntegrityError:
                messages.error(request, f"A workstation with code {ws.code!r} already exists.")
                return redirect("workstation-manage")
            messages.success(request, f"Workstation {ws.code} updated.")
            return redirect("workstation-manage")

    return render(
        request,
        "elections/workstation_manage.html",
        {
            "workstations": workstations,
            "elections": elections,
            "stations": stations,
            "purposes": Workstation.PURPOSE_CHOICES,
            "statuses": Workstation.STATUS_CHOICES,
        },
    )


@require_http_methods(["GET", "POST"])
def workstation_login(request):
    if request.method == "GET":
        return render(request, "elections/workstation_login.html", {})
    code = request.POST.get("code", "").strip()
    secret = request.POST.get("credential", request.POST.get("secret", "")).strip()
    ws = authenticate_workstation(code, secret)
    if ws is None:
        from django.contrib import messages
        messages.error(request, "Invalid or inactive workstation credentials.")
        return render(request, "elections/workstation_login.html", {}, status=403)
    login_workstation(request, ws)
    # Redirect based on purpose.
    if ws.purpose == Workstation.PURPOSE_VERIFICATION:
        return redirect("workstation-verification")
    return redirect("workstation-voting")


@require_POST
def workstation_logout(request):
    logout_workstation(request)
    return redirect("workstation-login")


def workstation_voting(request):
    ws, redir = require_workstation(request, Workstation.PURPOSE_VOTING)
    if redir:
        return redir
    # Dedicated kiosk entry point: send the voter straight to the assigned
    # election's login page when one is configured.
    if ws.election_id:
        return redirect("voter_login", pk=ws.election_id)
    return render(
        request,
        "elections/workstation_voting.html",
        {"workstation": ws},
    )


def workstation_verification(request):
    ws, redir = require_workstation(request, Workstation.PURPOSE_VERIFICATION)
    if redir:
        return redir
    if not request.user.is_authenticated:
        return redirect("accounts:login" if False else "/watch/login/?next=/workstation/verification/")
    # authorized officer session check: staff or org member
    if not (request.user.is_staff or request.user.is_superuser):
        raise PermissionDenied("Authorized officer session required.")
    from django.contrib import messages

    # Don't let flash messages pop up at the top of this kiosk screen.
    list(messages.get_messages(request))

    voter = None
    result = None
    voter_id = ""
    election = ws.election
    if request.method == "POST":
        voter_id = request.POST.get("voter_id", "").strip()
        election_id = request.POST.get("election")
        if election_id:
            election = get_object_or_404(Election, pk=election_id)
        # lookup voter (case-insensitive ID, scoped to the selected election)
        qs = Voter.objects.filter(voter_id__iexact=voter_id)
        if election is not None:
            qs = qs.filter(election=election)
        voter = qs.first()
        if voter is not None and election is None:
            election = voter.election
        issue = request.POST.get("issue_token") == "1"
        retrieve = request.POST.get("retrieve_token") == "1"
        revoke = request.POST.get("revoke_token") == "1"
        if issue or retrieve or revoke:
            if voter is None:
                result = _verification_error("Voter not found for this election.")
            elif revoke:
                result = revoke_voter_token(
                    voter,
                    actor=request.user,
                    request=request,
                    reason="Token revoked at verification workstation.",
                )
            else:
                try:
                    result = issue_voter_token(
                        voter,
                        election=election or voter.election,
                        actor=request.user,
                        request=request,
                        allow_reissue=retrieve,
                        reason=(
                            "Token replaced at verification workstation."
                            if retrieve
                            else ""
                        ),
                    )
                except TokenIssuanceError as exc:
                    result = _verification_error(str(exc), voter)

    if result is not None:
        # The token result panel already shows this outcome; do not repeat it
        # in the page-level message strip, which would push the kiosk card and
        # make the form scroll.
        list(messages.get_messages(request))

    voter_eligible = None
    if voter is not None:
        voter_eligible = voter.eligible_for(election or voter.election)

    verification_token = None
    if voter is not None:
        verification_token = _verification_token_state(
            voter, election or voter.election
        )

    elections = Election.objects.order_by("-created_at")[:200]
    return render(
        request,
        "elections/workstation_verification.html",
        {
            "workstation": ws,
            "election": election,
            "elections": elections,
            "voter": voter,
            "voter_id": voter_id,
            "result": result,
            "voter_eligible": voter_eligible,
            "verification_token": verification_token,
            "background_url": _verification_background_url(),
        },
    )