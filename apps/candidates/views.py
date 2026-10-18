"""Candidate registration/approval + representative management views."""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.core.permissions import is_org_official, require_org_admin, user_orgs
from apps.elections.models import Election
from .forms import CandidateForm, RepresentativeApprovalForm, RepresentativeRegistrationForm
from .models import Candidate, Incident, Representative

User = get_user_model()


def _scoped_election(request, slug) -> Election:
    qs = Election.objects.select_related("organization")
    if not request.user.is_superuser:
        org_ids = user_orgs(request.user).values_list("organization_id", flat=True)
        qs = qs.filter(organization_id__in=org_ids)
    return get_object_or_404(qs, slug=slug)


@login_required
@never_cache
def candidate_list(request, slug):
    election = _scoped_election(request, slug)
    if not is_org_official(request.user, election.organization):
        raise PermissionDenied
    candidates = (
        election.candidates.select_related("position")
        .order_by("position__order", "order", "full_name")
    )
    return render(
        request,
        "candidates/list.html",
        {"election": election, "candidates": candidates},
    )


@login_required
@require_http_methods(["GET", "POST"])
def candidate_create(request, slug):
    election = _scoped_election(request, slug)
    if not is_org_official(request.user, election.organization):
        raise PermissionDenied
    if election.config_frozen_at:
        messages.error(request, "Configuration is frozen; candidates cannot change.")
        return redirect("candidates:list", slug=slug)
    form = CandidateForm(request.POST or None, request.FILES or None)
    form.fields["position"].queryset = election.positions.all()
    if request.method == "POST" and form.is_valid():
        cand = form.save(commit=False)
        cand.election = election
        cand.submitted_by = request.user
        cand.save()
        messages.success(request, "Candidate added (submit for review).")
        return redirect("candidates:list", slug=slug)
    return render(
        request, "candidates/form.html", {"form": form, "election": election}
    )


@login_required
@require_http_methods(["POST"])
def candidate_review(request, slug, pk):
    election = _scoped_election(request, slug)
    require_org_admin(request.user, election.organization)
    candidate = get_object_or_404(Candidate, pk=pk, election=election)
    action = request.POST.get("action")
    note = request.POST.get("note", "")
    if election.config_frozen_at:
        messages.error(request, "Configuration is frozen; review is locked.")
        return redirect("candidates:list", slug=slug)
    mapping = {
        "submit": Candidate.Status.PENDING,
        "approve": Candidate.Status.APPROVED,
        "reject": Candidate.Status.REJECTED,
        "withdraw": Candidate.Status.WITHDRAWN,
    }
    if action in mapping:
        if action in ("approve", "reject"):
            candidate.reviewed_by = request.user
            candidate.review_note = note
        if action == "withdraw":
            candidate.withdrawal_note = note
        candidate.status = mapping[action]
        candidate.save()
        from apps.audit.services import record_audit

        record_audit(
            organization=election.organization,
            actor=request.user,
            action=f"candidate.{action}",
            object_type="Candidate",
            object_id=str(candidate.pk),
            payload={"election": election.slug, "candidate": candidate.slug},
        )
        messages.success(request, f"Candidate {action} recorded.")
    return redirect("candidates:list", slug=slug)


# ---------------------------------------------------------------------------
# Representatives
# ---------------------------------------------------------------------------
@never_cache
@require_http_methods(["GET", "POST"])
def rep_register(request, slug):
    """A representative registers for a specific candidate + election."""
    # Representatives register *before* having staff rights: election must be
    # discoverable org-wide (they are typically staff of the org already).
    election = get_object_or_404(Election, slug=slug, organization__is_active=True)
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    existing = Representative.objects.filter(
        user=request.user, election=election
    ).first()
    if existing:
        messages.info(request, "You already have a registration for this election.")
        return redirect("monitoring:rep-home")
    form = RepresentativeRegistrationForm(request.POST or None)
    form.fields["candidate"].queryset = election.candidates.filter(
        status=Candidate.Status.APPROVED
    )
    if request.method == "POST" and form.is_valid():
        rep = form.save(commit=False)
        rep.user = request.user
        rep.election = election
        rep.status = Representative.Status.PENDING
        rep.save()
        messages.success(
            request, "Registration submitted. Awaiting approval by an election official."
        )
        return redirect("monitoring:rep-home")
    return render(
        request, "candidates/rep_register.html", {"form": form, "election": election}
    )


@login_required
@never_cache
def rep_approvals(request, slug):
    election = _scoped_election(request, slug)
    if not is_org_official(request.user, election.organization):
        raise PermissionDenied
    reps = (
        election.representatives.select_related("user", "candidate", "approved_by")
        .order_by("status", "user__username")
    )
    return render(
        request, "candidates/rep_approvals.html", {"election": election, "reps": reps}
    )


@login_required
@require_http_methods(["POST"])
def rep_review(request, slug, pk):
    election = _scoped_election(request, slug)
    if not is_org_official(request.user, election.organization):
        raise PermissionDenied
    rep = get_object_or_404(Representative, pk=pk, election=election)
    action = request.POST.get("action")
    if action == "approve":
        rep.status = Representative.Status.APPROVED
        rep.approved_by = request.user
        rep.save(update_fields=["status", "approved_by", "updated_at"])
        messages.success(request, f"{rep.user} approved as representative.")
    elif action == "reject":
        rep.status = Representative.Status.REJECTED
        rep.save(update_fields=["status", "updated_at"])
    elif action == "suspend":
        rep.status = Representative.Status.SUSPENDED
        rep.save(update_fields=["status", "updated_at"])
        rep.session_epoch += 1  # kill active sessions
        rep.save(update_fields=["session_epoch"])
    elif action == "attendance":
        att = request.POST.get("attendance")
        if att in dict(Representative.Attendance.choices):
            rep.attendance = att
            from django.utils import timezone

            rep.last_seen_at = timezone.now()
            rep.save(update_fields=["attendance", "last_seen_at", "updated_at"])
    from apps.audit.services import record_audit

    record_audit(
        organization=election.organization,
        actor=request.user,
        action=f"representative.{action}",
        object_type="Representative",
        object_id=str(rep.pk),
        payload={"election": election.slug, "user": rep.user.username},
    )
    return redirect("candidates:rep-approvals", slug=slug)


@login_required
@never_cache
def incident_list(request, slug):
    election = _scoped_election(request, slug)
    if not is_org_official(request.user, election.organization):
        raise PermissionDenied
    incidents = election.incidents.select_related("filed_by", "representative")
    return render(
        request,
        "candidates/incidents.html",
        {"election": election, "incidents": incidents},
    )
