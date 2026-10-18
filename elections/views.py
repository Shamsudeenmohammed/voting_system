"""Election management and public voting views.

Every staff view is organization-scoped through ``organizations.roles``
predicates; no queryset is evaluated without an org restriction for
authenticated staff.  Ballot selections never traverse this module: casting
is delegated to the ``ballot`` app and results are only ever shown as
aggregates from ``tally`` snapshots.
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from audit import services as audit
from ballot.services import issue_authorization, revoke_credential
from organizations.models import Organization
from organizations.roles import (
    can_approve_election,
    can_manage_election,
    can_view_election,
    is_system_admin,
    organizations_for,
    user_org_roles,
)

from .config import SelectionError, compute_config_hash, invalidate_config_cache
from .forms import (
    ApprovalRequestForm,
    CandidateForm,
    ElectionForm,
    ElectionSearchForm,
    PollingStationForm,
    PositionForm,
    VoterBulkForm,
    VoterForm,
)
from .lifecycle import (
    DUAL_CONTROL,
    LifecycleError,
    TRANSITIONS,
    decide_approval,
    request_approval,
    sync_election_state,
    transition,
)
from .models import Candidate, Election, ElectionApproval, PollingStation, Position, Voter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# UI action name -> target state.  Dual-control edges (approve_config,
# reopen, decertify) are intentionally absent: they go through the
# request/decide approval flow instead of a direct transition.
ACTION_TARGETS: dict[str, str] = {
    "submit": Election.STATE_REGISTRATION,
    "request_review": Election.STATE_CONFIG_REVIEW,
    "return_draft": Election.STATE_DRAFT,
    "return_registration": Election.STATE_REGISTRATION,
    "schedule": Election.STATE_SCHEDULED,
    "open": Election.STATE_OPEN,
    "close": Election.STATE_CLOSED,
    "start_reconciliation": Election.STATE_RECONCILIATION,
    "complete_reconciliation": Election.STATE_REVIEW,
    "certify": Election.STATE_CERTIFIED,
    "publish": Election.STATE_PUBLISHED,
    "archive": Election.STATE_ARCHIVED,
}

ACTION_LABELS: dict[str, str] = {
    Election.STATE_REGISTRATION: "Open registration",
    Election.STATE_CONFIG_REVIEW: "Submit for configuration review",
    Election.STATE_DRAFT: "Return to draft",
    Election.STATE_APPROVED: "Approve configuration",
    Election.STATE_SCHEDULED: "Schedule election",
    Election.STATE_OPEN: "Open voting",
    Election.STATE_CLOSED: "Close voting",
    Election.STATE_RECONCILIATION: "Start reconciliation",
    Election.STATE_REVIEW: "Submit for results review",
    Election.STATE_CERTIFIED: "Certify results",
    Election.STATE_PUBLISHED: "Publish results",
    Election.STATE_ARCHIVED: "Archive election",
}

DUAL_KIND_FOR_EDGE: dict[tuple[str, str], str] = dict(DUAL_CONTROL)


def available_transitions(election) -> list[dict]:
    """State-machine-derived action list for the current state (UI hints only)."""
    actions = []
    for target in sorted(TRANSITIONS.get(election.state, set())):
        dual = DUAL_KIND_FOR_EDGE.get((election.state, target))
        actions.append(
            {
                "target": target,
                "label": ACTION_LABELS.get(target, target.title()),
                "dual": dual,
            }
        )
    return actions


def _scoped_election(user, election_id, *, manage: bool = False) -> Election:
    """Fetch an election enforcing org scope for the requesting user."""
    election = get_object_or_404(
        Election.objects.select_related("organization"), pk=election_id
    )
    allowed = can_manage_election(user, election) if manage else can_view_election(user, election)
    if not allowed:
        raise PermissionDenied("You do not have access to this election.")
    return election


def _staff_elections(user, *, manage: bool = False):
    qs = Election.objects.select_related("organization").annotate(
        voter_count=Count("voters", distinct=True),
        candidate_count=Count("positions__candidates", distinct=True),
    )
    if is_system_admin(user):
        return qs.all()
    if manage:
        return qs.filter(organization__in=organizations_for(user))
    return qs.filter(organization__in=Organization.objects.filter(memberships__user=user))


# ---------------------------------------------------------------------------
# public surfaces
# ---------------------------------------------------------------------------

def home(request):
    """Public landing page: open/scheduled/published elections only."""
    now = timezone.now()
    public_states = (Election.STATE_OPEN, Election.STATE_SCHEDULED, Election.STATE_PUBLISHED)
    elections = (
        Election.objects.filter(state__in=public_states)
        .select_related("organization")
        .order_by("-start_at")[:50]
    )
    form = ElectionSearchForm(request.GET or None)
    if form.is_valid() and form.cleaned_data.get("q"):
        elections = [e for e in elections if form.cleaned_data["q"].lower() in e.title.lower()]
    return render(
        request,
        "elections/home.html",
        {
            "elections": elections,
            "now": now,
            "form": form,
            "user_roles": user_org_roles(request.user) if request.user.is_authenticated else {},
        },
    )


def election_detail(request, pk):
    """Public detail page for a single election."""
    if request.user.is_authenticated:
        election = _scoped_election(request.user, pk)
    else:
        election = get_object_or_404(
            Election.objects.select_related("organization"),
            pk=pk,
            state__in=[
                Election.STATE_OPEN,
                Election.STATE_SCHEDULED,
                Election.STATE_PUBLISHED,
                Election.STATE_CLOSED,
                Election.STATE_CERTIFIED,
            ],
        )
    try:
        sync_election_state(election)
    except LifecycleError:  # pragma: no cover - schedule races are harmless here
        pass

    positions = [
        {
            "position": pos,
            "candidates": [c for c in pos.candidates.all() if c.is_on_ballot],
        }
        for pos in election.positions.prefetch_related("candidates").order_by("order", "id")
    ]
    from tally.services import latest_snapshot

    snapshot = latest_snapshot(election)
    results_visible = election.state in (Election.STATE_PUBLISHED, Election.STATE_ARCHIVED)
    ctx = {
        "election": election,
        "positions": positions,
        "snapshot": snapshot,
        "results_visible": results_visible and snapshot is not None,
        "open_for_voting": election.accepts_ballots and timezone.now() <= election.end_at,
        "can_manage": can_manage_election(request.user, election),
        "user_roles": user_org_roles(request.user) if request.user.is_authenticated else {},
    }
    return render(request, "elections/election_detail.html", ctx)


@require_http_methods(["GET", "POST"])
def voter_access(request, pk):
    """Anonymous voter access point.

    A single-use ballot authorization is issued for a valid voter id and
    displayed exactly once (in production this page is the hand-off point for
    out-of-band delivery, e.g. printed slip or SMS).  Failure behaviour is
    uniform and nothing is written to application logs.
    """
    election = get_object_or_404(Election.objects.select_related("organization"), pk=pk)
    if not election.accepts_ballots or timezone.now() > election.end_at:
        messages.error(request, "Voting is not currently open for this election.")
        return redirect("election_detail", pk=pk)

    if request.method == "POST":
        voter_id = request.POST.get("voter_id", "").strip()
        voter = None
        if voter_id:
            voter = Voter.objects.filter(
                election=election, voter_id__iexact=voter_id, status=Voter.STATUS_ACTIVE
            ).first()
        if voter is None:
            messages.error(request, "This voter ID is not authorised for this election.")
            return render(
                request,
                "elections/voter_access.html",
                {"election": election, "issued": None},
            )
        _authorization, token = issue_authorization(voter)
        audit.record(
            action="credential_issued",
            election=election,
            organization=election.organization,
            request=request,
            object_type="voter",
            object_id=str(voter.pk),
            details={"voter_id_hash": audit.hash_ip(voter.voter_id)},
        )
        return render(
            request,
            "elections/voter_access.html",
            {
                "election": election,
                "issued": True,
                "token": token,
                "ballot_url": request.build_absolute_uri(
                    reverse("ballot_prepare", args=[token])
                ),
            },
        )

    return render(request, "elections/voter_access.html", {"election": election, "issued": None})


# ---------------------------------------------------------------------------
# management dashboard
# ---------------------------------------------------------------------------

@staff_member_required
def dashboard(request):
    form = ElectionSearchForm(request.GET or None)
    elections = _staff_elections(request.user).order_by("-created_at")
    manage_ids = set(_staff_elections(request.user, manage=True).values_list("id", flat=True))
    if form.is_valid():
        if form.cleaned_data.get("q"):
            elections = elections.filter(title__icontains=form.cleaned_data["q"])
        if form.cleaned_data.get("state"):
            elections = elections.filter(state=form.cleaned_data["state"])
    return render(
        request,
        "elections/dashboard.html",
        {
            "elections": elections,
            "form": form,
            "manage_ids": manage_ids,
            "orgs": organizations_for(request.user) if request.user.is_superuser else None,
        },
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def election_create(request):
    orgs = organizations_for(request.user)
    if not orgs.exists():
        return HttpResponseForbidden("You belong to no organization.")
    if request.method == "POST":
        form = ElectionForm(request.POST)
        if request.user.is_superuser and request.POST.get("organization"):
            org = get_object_or_404(Organization, pk=request.POST["organization"])
        else:
            org = orgs.first()
        if form.is_valid():
            election = form.save(commit=False)
            election.organization = org
            election.created_by = request.user
            election.save()
            audit.record(
                action="election_created",
                actor=request.user,
                election=election,
                organization=org,
                request=request,
                details={"title": election.title},
            )
            messages.success(request, "Election draft created.")
            return redirect("election_edit", pk=election.pk)
    else:
        form = ElectionForm()
    return render(
        request,
        "elections/election_form.html",
        {"form": form, "title": "Create election", "orgs": orgs if request.user.is_superuser else None},
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def election_edit(request, pk):
    election = _scoped_election(request.user, pk, manage=True)
    if election.state != Election.STATE_DRAFT:
        messages.error(request, "Only elections in DRAFT may be edited.")
        return redirect("election_config", pk=election.pk)
    if request.method == "POST":
        form = ElectionForm(request.POST, instance=election)
        if form.is_valid():
            form.save()
            invalidate_config_cache(election)
            messages.success(request, "Election updated.")
            return redirect("election_edit", pk=election.pk)
    else:
        form = ElectionForm(instance=election)
    return render(
        request,
        "elections/election_form.html",
        {"form": form, "title": f"Edit {election.title}", "election": election, "orgs": None},
    )


@staff_member_required
def election_config(request, pk):
    """Configuration surface: positions, candidates, stations, approvals."""
    election = _scoped_election(request.user, pk, manage=True)
    positions = list(election.positions.prefetch_related("candidates").order_by("order", "id"))
    stations = election.stations.order_by("name")
    try:
        config_hash = compute_config_hash(election)
    except SelectionError as exc:  # pragma: no cover - defensive
        config_hash = None
        messages.warning(request, str(exc))
    pending = election.approvals.filter(status=ElectionApproval.STATUS_PENDING)
    return render(
        request,
        "elections/election_config.html",
        {
            "election": election,
            "positions": positions,
            "stations": stations,
            "position_form": PositionForm(),
            "candidate_form": CandidateForm(),
            "station_form": PollingStationForm(),
            "config_hash": config_hash,
            "transitions": available_transitions(election),
            "pending_approvals": pending,
            "approval_form": ApprovalRequestForm(),
            "can_approve": can_approve_election(request.user, election),
            "dual_kinds": DUAL_KIND_FOR_EDGE,
        },
    )


@staff_member_required
@require_http_methods(["POST"])
def position_add(request, pk):
    election = _scoped_election(request.user, pk, manage=True)
    if election.state != Election.STATE_DRAFT:
        messages.error(request, "The ballot structure can only be changed while in DRAFT.")
        return redirect("election_config", pk=election.pk)
    form = PositionForm(request.POST)
    if form.is_valid():
        position = form.save(commit=False)
        position.election = election
        position.save()
        invalidate_config_cache(election)
        audit.record(
            action="position_added",
            actor=request.user,
            election=election,
            request=request,
            details={"position_id": str(position.pk), "title": position.title},
        )
        messages.success(request, f"Position “{position.title}” added.")
    else:
        messages.error(request, form.errors.as_text())
    return redirect("election_config", pk=election.pk)


@staff_member_required
@require_http_methods(["POST"])
def position_delete(request, pk, position_id):
    election = _scoped_election(request.user, pk, manage=True)
    if election.state != Election.STATE_DRAFT:
        messages.error(request, "The ballot structure can only be changed while in DRAFT.")
        return redirect("election_config", pk=election.pk)
    position = get_object_or_404(Position, pk=position_id, election=election)
    title = position.title
    position.delete()
    invalidate_config_cache(election)
    audit.record(
        action="position_removed",
        actor=request.user,
        election=election,
        request=request,
        details={"title": title},
    )
    messages.success(request, f"Position “{title}” removed.")
    return redirect("election_config", pk=election.pk)


@staff_member_required
@require_http_methods(["POST"])
def candidate_add(request, pk, position_id):
    election = _scoped_election(request.user, pk, manage=True)
    if election.state != Election.STATE_DRAFT:
        messages.error(request, "Candidates can only be changed while in DRAFT.")
        return redirect("election_config", pk=election.pk)
    position = get_object_or_404(Position, pk=position_id, election=election)
    form = CandidateForm(request.POST)
    if form.is_valid():
        candidate = form.save(commit=False)
        candidate.position = position
        candidate.save()
        invalidate_config_cache(election)
        audit.record(
            action="candidate_added",
            actor=request.user,
            election=election,
            request=request,
            details={"candidate_id": str(candidate.pk), "full_name": candidate.full_name},
        )
        messages.success(request, f"Candidate “{candidate.full_name}” added.")
    else:
        messages.error(request, form.errors.as_text())
    return redirect("election_config", pk=election.pk)


@staff_member_required
@require_http_methods(["POST"])
def candidate_delete(request, pk, candidate_id):
    election = _scoped_election(request.user, pk, manage=True)
    if election.state != Election.STATE_DRAFT:
        messages.error(request, "Candidates can only be changed while in DRAFT.")
        return redirect("election_config", pk=election.pk)
    candidate = get_object_or_404(Candidate, pk=candidate_id, position__election=election)
    name = candidate.full_name
    candidate.delete()
    invalidate_config_cache(election)
    audit.record(
        action="candidate_removed",
        actor=request.user,
        election=election,
        request=request,
        details={"full_name": name},
    )
    messages.success(request, f"Candidate “{name}” removed.")
    return redirect("election_config", pk=election.pk)


@staff_member_required
@require_http_methods(["POST"])
def station_add(request, pk):
    election = _scoped_election(request.user, pk, manage=True)
    form = PollingStationForm(request.POST)
    if form.is_valid():
        station = form.save(commit=False)
        station.election = election
        station.save()
        messages.success(request, "Polling station added.")
    else:
        messages.error(request, form.errors.as_text())
    return redirect("election_config", pk=election.pk)


# ---------------------------------------------------------------------------
# voter registry
# ---------------------------------------------------------------------------

@staff_member_required
def election_voters(request, pk):
    election = _scoped_election(request.user, pk, manage=True)
    voters = election.voters.order_by("voter_id", "full_name")
    return render(
        request,
        "elections/election_voters.html",
        {
            "election": election,
            "voters": voters,
            "bulk_form": VoterBulkForm(),
            "voter_form": VoterForm(),
            "registration_open": election.state == Election.STATE_REGISTRATION,
        },
    )


@staff_member_required
@require_http_methods(["POST"])
def voter_add(request, pk):
    election = _scoped_election(request.user, pk, manage=True)
    if election.state != Election.STATE_REGISTRATION:
        messages.error(request, "Voters can only be added during REGISTRATION.")
        return redirect("election_voters", pk=election.pk)
    form = VoterForm(request.POST)
    if form.is_valid():
        voter = form.save(commit=False)
        voter.election = election
        voter.save()
        audit.record(
            action="voter_registered",
            actor=request.user,
            election=election,
            organization=election.organization,
            request=request,
            object_type="voter",
            object_id=str(voter.pk),
        )
        messages.success(request, f"Voter {voter.voter_id or voter.full_name} registered.")
    else:
        messages.error(request, form.errors.as_text())
    return redirect("election_voters", pk=election.pk)


@staff_member_required
@require_http_methods(["POST"])
def voter_bulk_add(request, pk):
    election = _scoped_election(request.user, pk, manage=True)
    if election.state != Election.STATE_REGISTRATION:
        messages.error(request, "Voters can only be added during REGISTRATION.")
        return redirect("election_voters", pk=election.pk)
    form = VoterBulkForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Bulk upload rejected: " + form.errors.as_text())
        return redirect("election_voters", pk=election.pk)

    created: list[Voter] = []
    with transaction.atomic():
        for row in form.rows:
            created.append(Voter.objects.create(election=election, **row))
        audit.record(
            action="voters_registered",
            actor=request.user,
            election=election,
            organization=election.organization,
            request=request,
            details={"count": len(created)},
        )

    issued: list[dict] = []
    if form.cleaned_data.get("issue_credentials"):
        for voter in created:
            _auth, token = issue_authorization(voter, actor=request.user)
            issued.append({"voter_id": voter.voter_id, "token": token})

    if issued:
        # Tokens are displayed exactly once, outside the persistent registry.
        return render(
            request,
            "elections/credentials_once.html",
            {"election": election, "issued": issued},
        )
    messages.success(request, f"{len(created)} voters registered.")
    return redirect("election_voters", pk=election.pk)


@staff_member_required
@require_http_methods(["POST"])
def voter_revoke(request, pk, voter_pk):
    election = _scoped_election(request.user, pk, manage=True)
    voter = get_object_or_404(Voter, pk=voter_pk, election=election)
    revoked = revoke_credential(
        voter, actor=request.user, reason="Registry correction", request=request
    )
    messages.success(
        request, f"{revoked} unused credential(s) revoked. Cast ballots are unaffected."
    )
    return redirect("election_voters", pk=election.pk)


# ---------------------------------------------------------------------------
# lifecycle actions
# ---------------------------------------------------------------------------

@staff_member_required
@require_http_methods(["POST"])
def election_action(request, pk):
    """Single entry point for every direct lifecycle transition.

    Authorisation for each action is enforced inside the lifecycle module
    (four-eyes included); this view only maps outcomes to user feedback.
    """
    election = _scoped_election(request.user, pk, manage=True)
    action = request.POST.get("action", "")
    target = ACTION_TARGETS.get(action)
    if target is None:
        messages.error(request, "Unknown action.")
        return redirect("election_config", pk=election.pk)

    if (election.state, target) in DUAL_KIND_FOR_EDGE:
        messages.error(
            request,
            "This transition requires a four-eyes approval request with a "
            "documented reason; use the approval panel below.",
        )
        return redirect("election_config", pk=election.pk)

    try:
        transition(
            election,
            target,
            actor=request.user,
            request=request,
            reason=request.POST.get("note", "") or "",
        )
    except LifecycleError as exc:
        messages.error(request, exc.message)
        return redirect("election_config", pk=election.pk)

    messages.success(request, f"Election is now {election.state.upper()}.")
    return redirect("election_config", pk=election.pk)


@staff_member_required
@require_http_methods(["POST"])
def approval_request_view(request, pk):
    """Create a four-eyes approval request (approve_config/reopen/decertify)."""
    election = _scoped_election(request.user, pk, manage=True)
    form = ApprovalRequestForm(request.POST)
    kind = request.POST.get("kind", "")
    if kind not in ("approve_config", "reopen", "decertify"):
        messages.error(request, "Unknown approval kind.")
        return redirect("election_config", pk=election.pk)
    if not form.is_valid():
        messages.error(request, "A documented reason is mandatory.")
        return redirect("election_config", pk=election.pk)
    try:
        approval = request_approval(
            election, kind, actor=request.user, reason=form.cleaned_data["reason"], request=request
        )
    except LifecycleError as exc:
        messages.error(request, exc.message)
        return redirect("election_config", pk=election.pk)
    messages.success(
        request,
        f"Approval request #{str(approval.pk)[:8]} submitted. A different "
        "administrator must decide it.",
    )
    return redirect("election_config", pk=election.pk)


@staff_member_required
@require_http_methods(["POST"])
def approval_decide_view(request, approval_pk):
    """Approve or reject a pending request (four-eyes enforced server-side)."""
    approval = get_object_or_404(ElectionApproval, pk=approval_pk)
    election = _scoped_election(request.user, approval.election_id, manage=True)
    approve = request.POST.get("decision") == "approve"
    try:
        approval = decide_approval(
            approval.pk, actor=request.user, approve=approve,
            note=request.POST.get("note", ""), request=request,
        )
    except LifecycleError as exc:
        messages.error(request, exc.message)
        return redirect("election_config", pk=election.pk)
    if approval.status == ElectionApproval.STATUS_APPROVED:
        messages.success(request, "Approved; the transition has been applied.")
    else:
        messages.success(request, "Request rejected.")
    return redirect("election_config", pk=election.pk)


# ---------------------------------------------------------------------------
# audit trail surface
# ---------------------------------------------------------------------------

@staff_member_required
def election_audit(request, pk):
    election = _scoped_election(request.user, pk)
    if not can_view_election(request.user, election):
        return HttpResponseForbidden()
    events = audit.AuditEvent.objects.filter(election=election).order_by("-seq")
    paginator = Paginator(events, 25)
    page = paginator.get_page(request.GET.get("page") or 1)
    chain = audit.verify_chain(f"election:{election.pk}")
    return render(
        request,
        "elections/election_audit.html",
        {
            "election": election,
            "page": page,
            "chain": chain,
            "transitions": available_transitions(election),
        },
    )


@staff_member_required
@require_http_methods(["POST"])
def audit_checkpoint(request, pk):
    election = _scoped_election(request.user, pk)
    if not (is_system_admin(request.user) or can_approve_election(request.user, election)):
        return HttpResponseForbidden()
    cp = audit.create_checkpoint(f"election:{election.pk}", actor=request.user)
    audit.record(
        action="audit_checkpoint_created",
        actor=request.user,
        election=election,
        request=request,
        object_type="checkpoint",
        object_id=str(cp.pk),
        details={"seq": cp.seq},
    )
    messages.success(request, f"Audit checkpoint created at sequence {cp.seq}.")
    return redirect("election_audit", pk=election.pk)
