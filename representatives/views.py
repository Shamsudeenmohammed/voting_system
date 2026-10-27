"""Representative accreditation, monitoring and incident surfaces.

Access rules (enforced here and in ``representatives/tests``):

* Representatives see only aggregate, policy-authorized data for **their
  own candidate** in **their own election** — never ballots, never voter
  credentials, never another candidate's restricted data.
* Approval/accreditation/revocation is an organization-staff action.
* Incident reports are visible to their reporter (status only) and fully to
  election staff.
"""
from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from audit import services as audit
from elections.models import Candidate, Election, PollingStation, Voter
from organizations.roles import (
    can_manage_election,
    can_view_live_totals,
    can_view_results_early,
    is_election_staff,
    is_system_admin,
)

from .models import AttendanceRecord, IncidentReport, Representative


def _my_representative(user) -> Representative | None:
    if not user or not user.is_authenticated:
        return None
    return (
        Representative.objects.filter(user=user, status=Representative.STATUS_APPROVED)
        .select_related("election", "candidate")
        .first()
    )


# ---------------------------------------------------------------------------
# public application
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "POST"])
def apply(request, pk):
    """Candidate representative application for one election."""
    election = get_object_or_404(Election, pk=pk)
    candidates = [
        c
        for pos in election.positions.prefetch_related("candidates")
        for c in pos.candidates.all()
        if c.is_on_ballot
    ]
    error = None
    if request.method == "POST":
        full_name = (request.POST.get("full_name") or "").strip()
        email = (request.POST.get("email") or "").strip()
        phone = (request.POST.get("phone") or "").strip()
        # NB: Candidate has no direct election FK - scope through its position,
        # and never 500 on a malformed/foreign candidate id from the form.
        candidate = None
        candidate_id = request.POST.get("candidate") or ""
        if candidate_id:
            try:
                candidate = Candidate.objects.filter(
                    pk=candidate_id, position__election=election
                ).first()
            except (ValidationError, ValueError):
                candidate = None
        if candidate is None:
            error = "Please choose a candidate for this election."
        elif not full_name or not email:
            error = "Name and email are required."
        elif Representative.objects.filter(
            election=election, candidate=candidate, email=email
        ).exclude(status=Representative.STATUS_REVOKED).exists():
            error = "An application with this email already exists for this candidate."
        else:
            rep = Representative.objects.create(
                election=election,
                candidate=candidate,
                user=request.user if request.user.is_authenticated else None,
                full_name=full_name,
                email=email,
                phone=phone,
            )
            audit.record(
                action="representative_applied",
                election=election,
                organization=election.organization,
                request=request,
                object_type="representative",
                object_id=str(rep.pk),
                details={"candidate_id": str(candidate.pk)},
            )
            messages.success(
                request, "Application submitted. You will be notified once it is approved."
            )
            return redirect("election_detail", pk=election.pk)
    return render(
        request,
        "representatives/apply.html",
        {"election": election, "candidates": candidates, "error": error},
    )


# ---------------------------------------------------------------------------
# staff actions
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
def decide(request, rep_pk):
    """Approve or revoke a representative (organization staff only)."""
    rep = get_object_or_404(
        Representative.objects.select_related("election"), pk=rep_pk
    )
    election = rep.election
    if not (is_system_admin(request.user) or can_manage_election(request.user, election)):
        raise PermissionDenied()
    decision = request.POST.get("decision")
    if decision == "approve":
        rep.status = Representative.STATUS_APPROVED
        rep.approved_by = request.user
        rep.approved_at = timezone.now()
        rep.user = rep.user or request.user
        rep.save(update_fields=["status", "approved_by", "approved_at", "user"])
        action = "representative_approved"
        messages.success(request, f"{rep.full_name} approved.")
    elif decision == "revoke":
        rep.status = Representative.STATUS_REVOKED
        rep.save(update_fields=["status"])
        action = "representative_revoked"
        messages.success(request, f"{rep.full_name} revoked.")
    else:
        messages.error(request, "Unknown decision.")
        return redirect("representatives_list", pk=election.pk)
    audit.record(
        action=action,
        actor=request.user,
        election=election,
        request=request,
        object_type="representative",
        object_id=str(rep.pk),
    )
    return redirect("representatives_list", pk=election.pk)


@require_http_methods(["POST"])
def accredit(request, rep_pk):
    """Record physical accreditation (presence) of an approved representative."""
    rep = get_object_or_404(Representative.objects.select_related("election"), pk=rep_pk)
    election = rep.election
    if not (is_system_admin(request.user) or can_manage_election(request.user, election)):
        raise PermissionDenied()
    if rep.status != Representative.STATUS_APPROVED:
        messages.error(request, "Only approved representatives can be accredited.")
        return redirect("representatives_list", pk=election.pk)
    AttendanceRecord.objects.create(representative=rep)
    if rep.accredited_at is None:
        rep.accredited_at = timezone.now()
        rep.save(update_fields=["accredited_at"])
    audit.record(
        action="representative_accredited",
        actor=request.user,
        election=election,
        request=request,
        object_type="representative",
        object_id=str(rep.pk),
    )
    messages.success(request, f"{rep.full_name} accredited on arrival.")
    return redirect("representatives_list", pk=election.pk)


@require_http_methods(["GET"])
def representatives_list(request, pk):
    election = get_object_or_404(Election.objects.select_related("organization"), pk=pk)
    if not is_election_staff(request.user, election):
        raise PermissionDenied()
    reps = (
        Representative.objects.filter(election=election)
        .select_related("candidate", "candidate__position")
        .annotate(attendance_count=Count("attendance"))
        .order_by("status", "full_name")
    )
    incidents = IncidentReport.objects.filter(election=election).order_by("-created_at")[:25]
    return render(
        request,
        "representatives/list.html",
        {"election": election, "reps": reps, "incidents": incidents},
    )


# ---------------------------------------------------------------------------
# representative dashboard (restricted surface)
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
def dashboard(request):
    """Aggregate-only monitoring surface for an approved representative."""
    rep = _my_representative(request.user)
    if rep is None:
        return render(request, "representatives/dashboard_empty.html", status=200)
    election = rep.election
    total = election.voters.filter(status=Voter.STATUS_ACTIVE).count()
    voted = election.voters.filter(status=Voter.STATUS_ACTIVE, has_voted=True).count()
    stations = (
        PollingStation.objects.filter(election=election)
        .annotate(ballot_count=Count("ballots"))
        .order_by("name")
    )
    attendance = rep.attendance.all()[:10]
    my_incidents = IncidentReport.objects.filter(election=election, reporter=rep)
    open_incidents = my_incidents.exclude(status__in=("resolved", "rejected")).count()
    ctx = {
        "rep": rep,
        "election": election,
        "turnout": {
            "total": total,
            "voted": voted,
            "percent": round(100 * voted / total, 1) if total else 0.0,
        },
        "stations": [
            {"station": s, "ballots": s.ballot_count}
            for s in stations
            # Only aggregate volumes per station — never ballot contents.
        ],
        "attendance": attendance,
        "my_incidents": my_incidents.select_related("station")[:10],
        "open_incidents": open_incidents,
        # Does the live monitor show turnout to this representative *now*?
        # Live turnout is withheld from non-privileged users while voting is
        # open, then shown once the election closes (mirrors live_status).
        "live_turnout_visible": can_view_results_early(request.user, election)
        or election.state != Election.STATE_OPEN,
        # Candidate totals follow the election's results-visibility policy.
        "live_board_visible": can_view_live_totals(request.user, election),
    }
    return render(request, "representatives/dashboard.html", ctx)


# ---------------------------------------------------------------------------
# incidents
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "POST"])
def incident_create(request, pk):
    election = get_object_or_404(Election.objects.select_related("organization"), pk=pk)
    rep = _my_representative(request.user)
    allowed_staff = is_system_admin(request.user) or can_manage_election(request.user, election)
    if not (allowed_staff or (rep and rep.election_id == election.pk)):
        raise PermissionDenied()
    if request.method == "POST":
        title = (request.POST.get("title") or "").strip()
        description = (request.POST.get("description") or "").strip()
        if not title or not description:
            messages.error(request, "Title and description are required.")
        else:
            station = PollingStation.objects.filter(
                pk=request.POST.get("station") or None, election=election
            ).first()
            incident = IncidentReport.objects.create(
                election=election,
                station=station,
                reporter=rep,
                reporter_user=None if rep else request.user,
                category=request.POST.get("category") or "incident",
                severity=request.POST.get("severity") or "medium",
                title=title,
                description=description,
            )
            audit.record(
                action="incident_reported",
                actor=request.user,
                election=election,
                request=request,
                object_type="incident",
                object_id=str(incident.pk),
                details={"category": incident.category, "severity": incident.severity},
            )
            messages.success(request, "Incident report submitted.")
            if allowed_staff:
                return redirect("representatives_list", pk=election.pk)
            return redirect("representative_dashboard")
    stations = election.stations.order_by("name")
    return render(
        request,
        "representatives/incident_form.html",
        {"election": election, "stations": stations},
    )


@require_http_methods(["GET"])
def incident_list(request, pk):
    election = get_object_or_404(Election.objects.select_related("organization"), pk=pk)
    rep = _my_representative(request.user)
    # NB: is_election_staff (not can_view_election) - an approved representative
    # passes can_view_election for their own election but must only ever see
    # their own reports below.
    staff = is_election_staff(request.user, election)
    if not staff and not (rep and rep.election_id == election.pk):
        raise PermissionDenied()
    incidents = IncidentReport.objects.filter(election=election).select_related(
        "station", "reporter"
    )
    if not staff:
        # Representatives only see their own reports.
        incidents = incidents.filter(reporter=rep)
    return render(
        request,
        "representatives/incidents.html",
        {"election": election, "incidents": incidents, "staff": staff},
    )


@require_http_methods(["POST"])
def incident_resolve(request, incident_pk):
    incident = get_object_or_404(
        IncidentReport.objects.select_related("election"), pk=incident_pk
    )
    election = incident.election
    if not (is_system_admin(request.user) or can_manage_election(request.user, election)):
        raise PermissionDenied()
    decision = request.POST.get("decision")
    if decision not in ("acknowledged", "resolved", "rejected"):
        messages.error(request, "Unknown decision.")
        return redirect("incident_list", pk=election.pk)
    incident.status = decision
    incident.resolution_note = request.POST.get("note", "")
    incident.resolved_by = request.user
    incident.resolved_at = timezone.now()
    incident.save(
        update_fields=["status", "resolution_note", "resolved_by", "resolved_at", "updated_at"]
    )
    audit.record(
        action=f"incident_{decision}",
        actor=request.user,
        election=election,
        request=request,
        object_type="incident",
        object_id=str(incident.pk),
    )
    messages.success(request, f"Incident marked {decision}.")
    return redirect("incident_list", pk=election.pk)
