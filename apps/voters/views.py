"""Voter-facing auth + official registry management views.

Voter sessions are separate from staff sessions: ``request.session`` gets a
``voter_auth`` key with a short grace; voter pages never render staff
menus, and staff pages never accept voter session data.
"""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.core.permissions import is_org_official, require_org_admin, user_orgs
from apps.elections.models import Election
from .forms import VoterImportForm, VoterLoginForm, VoterSearchForm
from .imports import run_import
from .models import EligibilityDecision, Voter, VoterCredential, VoterImportBatch
from .policy import eligibility_qs, is_eligible

VOTER_SESSION_KEY = "voter_auth"
MAX_VOTER_FAILURES = 5
LOCK_MINUTES = 15


def get_voter(request) -> Voter | None:
    data = request.session.get(VOTER_SESSION_KEY)
    if not data:
        return None
    voter = (
        Voter.objects.select_related("organization", "unit")
        .filter(pk=data.get("voter_id"), status=Voter.Status.ACTIVE)
        .first()
    )
    if voter is None:
        request.session.flush()
        return None
    # Session expiry check
    if data.get("auth_at", 0) + settings.SESSION_COOKIE_AGE < timezone.now().timestamp():
        request.session.flush()
        return None
    return voter


@never_cache
@require_http_methods(["GET", "POST"])
def voter_login(request):
    if request.session.get(VOTER_SESSION_KEY):
        return redirect("voters:home")
    form = VoterLoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        voter = form.voter
        cred, _ = VoterCredential.objects.get_or_create(voter=voter)
        if cred.is_locked:
            messages.error(
                request,
                "Account temporarily locked due to repeated failed attempts. "
                "Try again later or contact an election official.",
            )
        else:
            if form.cleaned["password_ok"]:
                request.session.cycle_key()
                request.session[VOTER_SESSION_KEY] = {
                    "voter_id": voter.pk,
                    "auth_at": int(timezone.now().timestamp()),
                }
                request.session.modified = True
                cred.failed_attempts = 0
                cred.locked_until = None
                cred.last_login_at = timezone.now()
                cred.save(
                    update_fields=[
                        "failed_attempts",
                        "locked_until",
                        "last_login_at",
                        "updated_at",
                    ]
                )
                return redirect("voters:home")
            cred.failed_attempts += 1
            if cred.failed_attempts >= MAX_VOTER_FAILURES:
                from datetime import timedelta

                cred.locked_until = timezone.now() + timedelta(minutes=LOCK_MINUTES)
            cred.save(update_fields=["failed_attempts", "locked_until", "updated_at"])
            messages.error(request, "Invalid credentials.")
    return render(request, "voters/login.html", {"form": form})


@require_http_methods(["POST"])
def voter_logout(request):
    request.session.pop(VOTER_SESSION_KEY, None)
    request.session.flush()
    return redirect("voters:voter-login")


@never_cache
def voter_home(request):
    voter = get_voter(request)
    if voter is None:
        return redirect("voters:voter-login")
    now = timezone.now()
    qs = (
        Election.objects.filter(
            organization=voter.organization,
            state__in=[
                Election.State.REGISTRATION,
                Election.State.APPROVED,
                Election.State.SCHEDULED,
                Election.State.OPEN,
            ],
        )
        .order_by("-scheduled_opens")
    )
    elections = []
    for el in qs:
        eligible, reason = is_eligible(el, voter)
        from apps.ballots.models import BallotGrant

        grant = BallotGrant.objects.filter(election=el, voter_ref=str(voter.pk)).first()
        elections.append(
            {
                "election": el,
                "eligible": eligible,
                "reason": reason,
                "can_start": eligible
                and el.state == Election.State.OPEN
                and (grant is None or grant.status != "consumed"),
                "voted": bool(grant and grant.status == "consumed"),
            }
        )
    return render(
        request,
        "voters/home.html",
        {"voter": voter, "elections": elections},
    )


# ---------------------------------------------------------------------------
# Registry management (officials)
# ---------------------------------------------------------------------------
def _org_or_403(request) -> "list[int]":
    org_ids = [m.organization_id for m in user_orgs(request.user)]
    if request.user.is_superuser:
        from apps.orgs.models import Organization

        org_ids = list(Organization.objects.values_list("id", flat=True))
    if not org_ids:
        raise PermissionDenied
    return org_ids


@login_required
@never_cache
def registry_home(request):
    org_ids = _org_or_403(request)
    batches = (
        VoterImportBatch.objects.filter(organization_id__in=org_ids)
        .select_related("organization", "uploaded_by")[:20]
    )
    counts = {
        "total": Voter.objects.filter(organization_id__in=org_ids).count(),
        "active": Voter.objects.filter(
            organization_id__in=org_ids, status=Voter.Status.ACTIVE
        ).count(),
        "verified": Voter.objects.filter(
            organization_id__in=org_ids, is_verified=True
        ).count(),
    }
    form = VoterImportForm()
    return render(
        request,
        "voters/registry.html",
        {"batches": batches, "counts": counts, "form": form},
    )


@login_required
@require_http_methods(["GET", "POST"])
def registry_list(request):
    org_ids = _org_or_403(request)
    form = VoterSearchForm(request.GET or None)
    qs = (
        Voter.objects.filter(organization_id__in=org_ids)
        .select_related("unit", "organization")
        .order_by("voter_number")
    )
    if form.is_valid():
        q = (form.cleaned_data.get("q") or "").strip()
        if q:
            qs = qs.filter(voter_number__icontains=q) | qs.filter(
                full_name__icontains=q
            )
        status = form.cleaned_data.get("status")
        if status:
            qs = qs.filter(status=status)
    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "voters/list.html",
        {"page": page, "form": form},
    )


@login_required
@require_http_methods(["POST"])
def registry_import(request):
    org_ids = _org_or_403(request)
    form = VoterImportForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Upload a CSV or XLSX file.")
        return redirect("voters:registry")
    org_id = form.cleaned_data["organization"].id
    if org_id not in org_ids:
        raise PermissionDenied
    batch = VoterImportBatch.objects.create(
        organization_id=org_id,
        uploaded_by=request.user,
        original_filename=request.FILES["file"].name[:255],
    )
    try:
        run_import(batch, request.FILES["file"])
        from apps.audit.services import record_audit

        record_audit(
            organization=batch.organization,
            actor=request.user,
            action="voters.imported",
            object_type="VoterImportBatch",
            object_id=str(batch.pk),
            payload={
                "total": batch.total_rows,
                "imported": batch.imported_rows,
                "rejected": batch.rejected_rows,
                "duplicates": batch.duplicate_rows,
            },
        )
        messages.success(
            request,
            f"Import complete: {batch.imported_rows} added, "
            f"{batch.duplicate_rows} duplicates, {batch.rejected_rows} rejected.",
        )
    except Exception as exc:
        batch.status = VoterImportBatch.Status.FAILED
        batch.error_report = [{"row": 0, "error": str(exc)[:500]}]
        batch.save(update_fields=["status", "error_report"])
        messages.error(request, f"Import failed: {exc}")
    return redirect("voters:registry")


@login_required
@never_cache
def import_report(request, batch_id):
    org_ids = _org_or_403(request)
    batch = get_object_or_404(VoterImportBatch, pk=batch_id, organization_id__in=org_ids)
    return render(request, "voters/import_report.html", {"batch": batch})


@login_required
@require_http_methods(["POST"])
def eligibility_decide(request, election_id, voter_id):
    org_ids = _org_or_403(request)
    election = get_object_or_404(Election, pk=election_id, organization_id__in=org_ids)
    voter = get_object_or_404(Voter, pk=voter_id, organization_id__in=org_ids)
    action = request.POST.get("action")
    reason = request.POST.get("reason", "").strip()
    if action not in dict(EligibilityDecision.Action.choices) or not reason:
        messages.error(request, "Action and reason are required.")
        return redirect("voters:registry")
    if election.voter_list_frozen:
        messages.error(
            request, "Voter list is frozen for this election; decisions are locked."
        )
        return redirect("voters:registry")
    EligibilityDecision.objects.create(
        election=election,
        voter=voter,
        action=action,
        reason=reason,
        decided_by=request.user,
    )
    if action == EligibilityDecision.Action.WITHDRAWN:
        voter.status = Voter.Status.WITHDRAWN
        voter.save(update_fields=["status", "updated_at"])
    elif action == EligibilityDecision.Action.CHALLENGED:
        voter.status = Voter.Status.CHALLENGED
        voter.save(update_fields=["status", "updated_at"])
    elif action == EligibilityDecision.Action.RESOLVED:
        voter.status = Voter.Status.ACTIVE
        voter.save(update_fields=["status", "updated_at"])
    from apps.audit.services import record_audit

    record_audit(
        organization=election.organization,
        actor=request.user,
        action="voters.eligibility_decision",
        object_type="Voter",
        object_id=str(voter.pk),
        payload={"election": election.slug, "decision": action, "reason": reason[:300]},
    )
    messages.success(request, "Eligibility decision recorded.")
    return redirect("voters:registry")
