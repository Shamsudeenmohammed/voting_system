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
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
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
    can_manage_officials,
    can_oversee_election,
    can_view_election,
    can_view_results_early,
    chair_organizations_for,
    console_required,
    has_system_wide_scope,
    is_system_admin,
    memberships,
    organizations_for,
    user_org_roles,
)

from . import governance
from . import readiness
from . import voter_import
from .config import SelectionError, compute_config_hash, invalidate_config_cache
from .forms import (
    ApprovalRequestForm,
    CandidateForm,
    ElectionDeleteForm,
    ElectionForm,
    ElectionGovernanceForm,
    ElectionSearchForm,
    OverrideForm,
    PollingStationForm,
    PositionForm,
    VoterBulkForm,
    VoterFileUploadForm,
    VoterForm,
)
from .lifecycle import (
    DUAL_CONTROL,
    LifecycleError,
    TRANSITIONS,
    admin_override,
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

# (source, target) -> the ``election_action`` name that carries it out.  Needed
# because two edges may share a target state (registration is reached from both
# DRAFT and CONFIG_REVIEW by different actions), so the target alone is
# ambiguous.
EDGE_ACTIONS: dict[tuple[str, str], str] = {
    (Election.STATE_DRAFT, Election.STATE_REGISTRATION): "submit",
    (Election.STATE_DRAFT, Election.STATE_ARCHIVED): "archive",
    (Election.STATE_REGISTRATION, Election.STATE_CONFIG_REVIEW): "request_review",
    (Election.STATE_REGISTRATION, Election.STATE_DRAFT): "return_draft",
    (Election.STATE_CONFIG_REVIEW, Election.STATE_REGISTRATION): "return_registration",
    (Election.STATE_APPROVED, Election.STATE_SCHEDULED): "schedule",
    (Election.STATE_APPROVED, Election.STATE_CONFIG_REVIEW): "request_review",
    (Election.STATE_SCHEDULED, Election.STATE_OPEN): "open",
    (Election.STATE_SCHEDULED, Election.STATE_ARCHIVED): "archive",
    (Election.STATE_OPEN, Election.STATE_CLOSED): "close",
    (Election.STATE_CLOSED, Election.STATE_RECONCILIATION): "start_reconciliation",
    (Election.STATE_RECONCILIATION, Election.STATE_REVIEW): "complete_reconciliation",
    (Election.STATE_REVIEW, Election.STATE_RECONCILIATION): "start_reconciliation",
    (Election.STATE_CERTIFIED, Election.STATE_PUBLISHED): "publish",
    (Election.STATE_PUBLISHED, Election.STATE_ARCHIVED): "archive",
}


def available_transitions(election) -> list[dict]:
    """State-machine-derived action list for the current state (UI hints only).

    ``dual`` is the *policy-aware* approval kind required for the edge (``None``
    when the election's governance policy has waived four-eyes for it).
    """
    actions = []
    for target in sorted(TRANSITIONS.get(election.state, set())):
        dual = governance.dual_control_kind(election, election.state, target)
        actions.append(
            {
                "target": target,
                "label": ACTION_LABELS.get(target, target.title()),
                "dual": dual,
                "action": EDGE_ACTIONS.get((election.state, target)),
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


def _form_error_text(form) -> str:
    """Flatten form errors into a single readable message.

    ``form.errors.as_text()`` produces a newline-bulleted dump that renders
    poorly in the message framework; this yields one line per field error,
    which keeps validation messages (e.g. passport photograph rejections)
    legible in the management UI.
    """
    parts = []
    for field, errors in form.errors.items():
        label = form[field].label if field in form.fields else "Form"
        for error in errors:
            parts.append(f"{label}: {error}" if label else str(error))
    return " ".join(parts)


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
    """Onsite-only entry point: send officials (and admins) to login.

    Public election browsing and remote-voting entry points are intentionally
    not provided; voting happens in person at registered polling stations.
    """
    if request.user.is_authenticated:
        from organizations.roles import is_system_admin

        if is_system_admin(request.user) or request.user.is_staff:
            return redirect("dashboard")
        return redirect("representative_dashboard")
    return redirect("login")


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
                {"election": election, "issued": None, "status": "unregistered",
                 "message": "This voter is not registered for this election."},
            )
        from ballot.services import issue_voter_token

        result = issue_voter_token(voter, election=election, actor=request.user, request=request)
        if result.status == "already_voted":
            messages.error(request, result.message)
            return render(
                request,
                "elections/voter_access.html",
                {"election": election, "issued": False, "status": result.status,
                 "message": result.message},
            )
        if result.status in ("ineligible", "election_closed", "unregistered"):
            messages.error(request, result.message)
            return render(
                request,
                "elections/voter_access.html",
                {"election": election, "issued": False, "status": result.status,
                 "message": result.message},
            )
        if result.status == "active_token":
            messages.info(request, result.message)
            return render(
                request,
                "elections/voter_access.html",
                {"election": election, "issued": False, "status": result.status,
                 "message": result.message, "authorization": result.authorization},
            )
        token = result.token
        return render(
            request,
            "elections/voter_access.html",
            {
                "election": election,
                "issued": True,
                "token": token,
                "status": result.status,
                "message": result.message,
                "ballot_url": request.build_absolute_uri(
                    reverse("ballot_prepare", args=[token])
                ),
            },
        )

    return render(request, "elections/voter_access.html", {"election": election, "issued": None})


# ---------------------------------------------------------------------------
# Dedicated voter login (Voter ID + 6-char token)
# ---------------------------------------------------------------------------


class VoterLoginError(Exception):
    """Voter login failure with a safe, user-facing message."""

    def __init__(self, message: str, code: str = "login_failed"):
        super().__init__(message)
        self.message = message
        self.code = code


def _get_throttle_key(request, election) -> str:
    """Generate the cache key for voter login throttling."""
    ip = (
        request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        or request.META.get("REMOTE_ADDR", "")
        or "unknown"
    )
    return f"throttle:voter_login:{election.pk}:{ip}"


def _throttle_voter_login(request, election) -> bool:
    """Check if voter login attempts are rate-limited (without incrementing).

    Returns True if throttled (too many attempts).
    """
    from django.core.cache import cache
    from django.conf import settings

    key = _get_throttle_key(request, election)
    limit = getattr(settings, "RATE_LIMIT_VOTER_LOGIN", 8)
    count = cache.get(key, 0)
    return count >= limit


def _record_failed_login(request, election):
    """Increment the failed login counter for this IP + election."""
    from django.core.cache import cache
    from django.conf import settings

    key = _get_throttle_key(request, election)
    window = getattr(settings, "RATE_LIMIT_WINDOW", 60)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=window)


def _clear_login_throttle(request, election):
    """Clear the throttle counter on successful login."""
    from django.core.cache import cache

    ip = (
        request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        or request.META.get("REMOTE_ADDR", "")
        or "unknown"
    )
    key = f"throttle:voter_login:{election.pk}:{ip}"
    cache.delete(key)


def _verify_voter_credentials(election, voter_id: str, token: str):
    """Verify voter ID and token without revealing whether voter ID exists.

    Returns (voter, authorization) on success, raises VoterLoginError on failure.
    """
    from ballot import crypto
    from ballot.models import BallotAuthorization
    from elections.models import Voter

    # Normalize token for lookup (uppercase for alphanumeric)
    normalized_token = token.upper() if election.token_format == "alphanumeric" else token
    if not crypto.validate_token_format(normalized_token, election.token_format):
        # Don't reveal format details in error
        raise VoterLoginError("Invalid voting credential format.")

    token_hash = crypto.hash_token(normalized_token)

    # Look up authorization by token hash only (constant-time wrt voter ID)
    auth = (
        BallotAuthorization.objects.select_related("election", "voter")
        .filter(token_hash=token_hash, election=election)
        .first()
    )

    if auth is None:
        # Uniform error: never reveal whether token or voter ID exists
        raise VoterLoginError("Invalid Voter ID or voting credential.")

    voter = auth.voter

    # Verify voter ID matches (case-insensitive for voter_id)
    if voter.voter_id.lower() != voter_id.lower():
        raise VoterLoginError("Invalid Voter ID or voting credential.")

    # Check voter status
    if voter.status != Voter.STATUS_ACTIVE:
        raise VoterLoginError("You are not eligible to vote in this election.")

    # Check eligibility (organization unit, etc.)
    if not voter.eligible_for(election):
        raise VoterLoginError("You are not eligible to vote in this election.")

    # Check authorization usability (not consumed, not revoked, not expired)
    if not auth.usable:
        if auth.consumed_at:
            raise VoterLoginError(
                "You have already cast your vote for this election. You cannot vote again.",
                code="already_voted",
            )
        if auth.revoked_at:
            raise VoterLoginError("This voting credential has been revoked.")
        if auth.expires_at and auth.expires_at < timezone.now():
            raise VoterLoginError("This voting credential has expired.")

    # Check election state
    if not election.accepts_ballots or timezone.now() > election.end_at:
        raise VoterLoginError("Voting is not currently open for this election.")

    return voter, auth


@require_http_methods(["GET", "POST"])
def voter_login(request, pk):
    """Dedicated voter login page: Voter ID + 6-char token.

    This is the single entry point for both online and polling-station voters.
    Polling officers verify identity first, then the voter uses this page.
    """
    election = get_object_or_404(Election.objects.select_related("organization"), pk=pk)

    # Election must be in a state where ballots can be accepted
    if not election.accepts_ballots or timezone.now() > election.end_at:
        return render(
            request,
            "elections/voter_login.html",
            {
                "election": election,
                "error": "Voting is not currently open for this election.",
                "error_code": "election_closed",
            },
            status=403,
        )

    if request.method == "POST":
        # Rate limiting
        if _throttle_voter_login(request, election):
            return render(
                request,
                "elections/voter_login.html",
                {
                    "election": election,
                    "error": "Too many login attempts. Please wait a moment and try again.",
                    "error_code": "rate_limited",
                },
                status=429,
            )

        voter_id = request.POST.get("voter_id", "").strip()
        token = request.POST.get("token", "").strip()

        if not voter_id or not token:
            _record_failed_login(request, election)
            return render(
                request,
                "elections/voter_login.html",
                {
                    "election": election,
                    "error": "Please enter both your Voter ID and voting credential.",
                    "error_code": "missing_fields",
                    "voter_id": voter_id,
                },
                status=400,
            )

        # Token length validation (frontend also validates, but backend is authoritative)
        if len(token) > 6:
            _record_failed_login(request, election)
            return render(
                request,
                "elections/voter_login.html",
                {
                    "election": election,
                    "error": "Voting credential must be 6 characters or fewer.",
                    "error_code": "token_too_long",
                    "voter_id": voter_id,
                },
                status=400,
            )

        try:
            voter, auth = _verify_voter_credentials(election, voter_id, token)
        except VoterLoginError as exc:
            _record_failed_login(request, election)
            return render(
                request,
                "elections/voter_login.html",
                {
                    "election": election,
                    "error": exc.message,
                    "error_code": exc.code,
                    "voter_id": voter_id,
                },
                status=403 if exc.code != "rate_limited" else 429,
            )

        # Successful login - clear throttle and redirect to ballot
        _clear_login_throttle(request, election)

        # Audit the successful login (without token)
        audit.record(
            action="voter_login",
            election=election,
            organization=election.organization,
            request=request,
            object_type="voter",
            object_id=str(voter.pk),
            details={"voter_id_hash": audit.hash_ip(voter.voter_id)},
        )

        # Redirect to ballot preparation with the token
        return redirect("ballot_prepare", token=token)

    # GET request - show login form
    return render(
        request,
        "elections/voter_login.html",
        {
            "election": election,
            "token_format": election.token_format,
            "token_max_length": 6,
        },
    )

ROLE_LABELS = {
    "org_admin": "Institution election administrator",
    "ec_chair": "Election commission chair",
    "election_official": "Election official",
    "auditor": "Auditor",
    "observer": "Observer",
}


def _dashboard_role(user) -> str:
    if is_system_admin(user):
        return "Super Admin"
    if has_system_wide_scope(user):
        return "Election commission chair (system-wide)"
    roles = {m.role for m in memberships(user)}
    for key in ("org_admin", "ec_chair", "election_official", "auditor", "observer"):
        if key in roles:
            return ROLE_LABELS[key]
    if getattr(user, "is_staff", False):
        return "Console user"
    return "User"


@console_required
def dashboard(request):
    form = ElectionSearchForm(request.GET or None)
    scoped = _staff_elections(request.user)
    elections = scoped.order_by("-created_at")
    manage_qs = _staff_elections(request.user, manage=True)
    manage_ids = set(manage_qs.values_list("id", flat=True))
    if form.is_valid():
        if form.cleaned_data.get("q"):
            elections = elections.filter(title__icontains=form.cleaned_data["q"])
        if form.cleaned_data.get("state"):
            elections = elections.filter(state=form.cleaned_data["state"])

    # "What needs attention": elections the user can act on whose readiness has
    # blocking issues (or, for approvers, a pending request they can decide).
    attention: list[dict] = []
    for election in manage_qs.order_by("-created_at"):
        report = readiness.assess(election)
        if report["attention"]:
            attention.append(
                {
                    "election": election,
                    "report": report,
                    "pending": election.approvals.filter(
                        status=ElectionApproval.STATUS_PENDING
                    ).count(),
                    "blocking": bool(report["blocking"]),
                }
            )
    can_approve_any = is_system_admin(request.user) or bool(
        {m.role for m in memberships(request.user)} & {"org_admin", "ec_chair"}
    )
    if can_approve_any:
        decidable_qs = ElectionApproval.objects.filter(
            status=ElectionApproval.STATUS_PENDING,
            election__in=elections,
        )
        if not is_system_admin(request.user):
            # Four-eyes: a Super Admin may decide their own request; everyone
            # else may only decide requests raised by somebody else.
            decidable_qs = decidable_qs.exclude(requested_by=request.user)
        decidable = decidable_qs.count()
    else:
        decidable = 0
    summary = scoped.aggregate(
        total=Count("id"),
        open=Count("id", filter=Q(state=Election.STATE_OPEN)),
        scheduled=Count("id", filter=Q(state=Election.STATE_SCHEDULED)),
    )
    summary["attention"] = len(attention)
    return render(
        request,
        "elections/dashboard.html",
        {
            "elections": elections,
            "form": form,
            "manage_ids": manage_ids,
            "orgs": organizations_for(request.user) if request.user.is_superuser else None,
            "role_label": _dashboard_role(request.user),
            "summary": summary,
            "attention": attention[:6],
            "attention_count": len(attention),
            "can_manage_any": bool(manage_ids),
            "can_approve_any": can_approve_any,
            "can_oversee_any": can_oversee_election(request.user, elections[0])
            if elections
            else is_system_admin(request.user) or has_system_wide_scope(request.user),
            "decidable_count": decidable,
        },
    )


@console_required
def chair_dashboard(request):
    """Dedicated election-commission-chair oversight dashboard.

    Shows, for every election the chair oversees: readiness, schedule, pending
    approvals they may decide, polling stations, accredited representatives and
    open incidents, plus the per-election governance policy.  Read-only - every
    action is taken on the election workspace (or the institutions console).
    """
    user = request.user
    if not (
        is_system_admin(user)
        or has_system_wide_scope(user)
        or chair_organizations_for(user).exists()
    ):
        raise PermissionDenied(
            "The election oversight dashboard is available to election "
            "commission chairs and the Super Admin."
        )

    overseeable = [
        election
        for election in _staff_elections(user).order_by("start_at")
        if can_oversee_election(user, election)
    ]
    rows: list[dict] = []
    for election in overseeable:
        report = readiness.assess(election)
        rows.append(
            {
                "election": election,
                "report": report,
                "pending": election.approvals.filter(
                    status=ElectionApproval.STATUS_PENDING
                ).count(),
                "stations": election.stations.count(),
                "officials": election.organization.memberships.filter(
                    role="election_official"
                ).count(),
                "representatives": election.representatives.filter(
                    status="approved"
                ).count(),
                "open_incidents": election.incidents.exclude(status="resolved").count(),
                "policy": governance.policy_summary(election),
                "can_certify": governance.can_certify(user, election),
                "can_publish": governance.can_publish(user, election),
            }
        )
    return render(
        request,
        "elections/chair_dashboard.html",
        {
            "rows": rows,
            "election_total": len(rows),
            "blocking_total": sum(1 for r in rows if r["report"]["blocking"]),
            "pending_total": sum(r["pending"] for r in rows),
            "incident_total": sum(r["open_incidents"] for r in rows),
        },
    )


@console_required
@require_http_methods(["GET", "POST"])
def election_create(request):
    orgs = organizations_for(request.user)
    if not orgs.exists():
        return HttpResponseForbidden("You belong to no organization.")
    show_org_picker = orgs.count() > 1
    default_org = orgs.first() if not show_org_picker else None

    if request.method == "POST":
        form = ElectionForm(request.POST)
        org_id = request.POST.get("organization") or ""
        org = orgs.filter(pk=org_id).first() if org_id else default_org
        if org is None:
            messages.error(
                request,
                "Choose the organization that owns this election.",
            )
        elif form.is_valid():
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
        {
            "form": form,
            "title": "Create election",
            "orgs": orgs if show_org_picker else None,
            "default_org": default_org,
        },
    )


@console_required
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


@console_required
def election_config(request, pk):
    """Election workspace: readiness, lifecycle actions, approvals, setup."""
    election = _scoped_election(request.user, pk, manage=True)
    positions = list(election.positions.prefetch_related("candidates").order_by("order", "id"))
    stations = election.stations.order_by("name")
    try:
        config_hash = compute_config_hash(election)
    except SelectionError as exc:  # pragma: no cover - defensive
        config_hash = None
        messages.warning(request, str(exc))

    transitions = available_transitions(election)
    can_approve = can_approve_election(request.user, election)
    is_super = is_system_admin(request.user)
    pending = list(
        election.approvals.filter(status=ElectionApproval.STATUS_PENDING).select_related(
            "requested_by"
        )
    )
    for approval in pending:
        # Four-eyes: the requester can never decide their own request - except
        # the Super Admin, who is never blocked by another person's approval.
        approval.can_decide = can_approve and (
            is_super or approval.requested_by_id != request.user.pk
        )

    approved = (
        election.approvals.filter(kind="approve_config", status=ElectionApproval.STATUS_APPROVED)
        .exists()
    )
    report = readiness.assess(election)
    can_govern = election.state == Election.STATE_DRAFT and (
        is_super or can_approve_election(request.user, election)
    )
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
            "transitions": [t for t in transitions if not t["dual"]],
            "dual_transitions": [t for t in transitions if t["dual"]],
            "pending_approvals": pending,
            "decided_approvals": (
                election.approvals.exclude(status=ElectionApproval.STATUS_PENDING)
                .select_related("requested_by", "decided_by")
                .order_by("-decided_at")[:6]
            ),
            "approval_form": ApprovalRequestForm(),
            "can_approve": can_approve,
            "approved_config": approved,
            "readiness": report,
            "next_action": report["next_action"],
            "dual_kinds": DUAL_KIND_FOR_EDGE,
            "can_edit_structure": election.state == Election.STATE_DRAFT,
            "can_edit_stations": election.state
            in (
                Election.STATE_DRAFT,
                Election.STATE_REGISTRATION,
                Election.STATE_CONFIG_REVIEW,
                Election.STATE_APPROVED,
                Election.STATE_SCHEDULED,
            ),
            "registration_open": election.state == Election.STATE_REGISTRATION,
            "voter_total": election.voters.count(),
            "voter_active": election.voters.filter(status="active").count(),
            # Governance / Super Admin controls (display hints only; the
            # server re-checks every one of these on submit).
            "governance_summary": governance.policy_summary(election),
            "governance_form": ElectionGovernanceForm(
                initial=ElectionGovernanceForm.initial_from(election)
            ),
            "can_govern": can_govern,
            "can_certify": governance.can_certify(request.user, election),
            "can_publish": governance.can_publish(request.user, election),
            "can_override": is_super,
            "can_oversee": can_oversee_election(request.user, election),
            "override_form": OverrideForm(election=election),
            "delete_form": ElectionDeleteForm(),
        },
    )


@console_required
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


@console_required
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


@console_required
@require_http_methods(["POST"])
def candidate_add(request, pk, position_id):
    election = _scoped_election(request.user, pk, manage=True)
    if election.state != Election.STATE_DRAFT:
        messages.error(request, "Candidates can only be changed while in DRAFT.")
        return redirect("election_config", pk=election.pk)
    position = get_object_or_404(Position, pk=position_id, election=election)
    form = CandidateForm(request.POST, request.FILES)
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
            details={
                "candidate_id": str(candidate.pk),
                "full_name": candidate.full_name,
                "photo_attached": bool(candidate.photo),
            },
        )
        messages.success(request, f"Candidate “{candidate.full_name}” added.")
    else:
        messages.error(request, _form_error_text(form))
    return redirect("election_config", pk=election.pk)


@console_required
@require_http_methods(["GET", "POST"])
def candidate_edit(request, pk, candidate_id):
    """Edit a candidate (including replacing/removing the passport photo).

    Read-only outside DRAFT, mirroring the candidate lifecycle rule that
    candidate details are frozen once the election leaves DRAFT.
    """
    election = _scoped_election(request.user, pk, manage=True)
    candidate = get_object_or_404(
        Candidate.objects.select_related("position"), pk=candidate_id, position__election=election
    )
    editable = election.state == Election.STATE_DRAFT

    if request.method == "POST":
        if not editable:
            messages.error(request, "Candidates can only be changed while in DRAFT.")
            return redirect("election_config", pk=election.pk)
        previous_photo_name = candidate.photo.name if candidate.photo else ""
        form = CandidateForm(request.POST, request.FILES, instance=candidate)
        if form.is_valid():
            candidate = form.save()
            # Photo change = new file stored, replaced, or explicitly cleared.
            photo_changed = bool(candidate.photo.name) != bool(previous_photo_name) or (
                bool(previous_photo_name) and candidate.photo.name != previous_photo_name
            )
            invalidate_config_cache(election)
            audit.record(
                action="candidate_updated",
                actor=request.user,
                election=election,
                request=request,
                details={
                    "candidate_id": str(candidate.pk),
                    "full_name": candidate.full_name,
                    "photo_changed": photo_changed,
                    "photo_attached": bool(candidate.photo),
                },
            )
            messages.success(request, f"Candidate “{candidate.full_name}” updated.")
            return redirect("election_config", pk=election.pk)
        messages.error(request, _form_error_text(form))
    else:
        form = CandidateForm(instance=candidate)

    return render(
        request,
        "elections/candidate_form.html",
        {
            "election": election,
            "candidate": candidate,
            "form": form,
            "editable": editable,
            "photo_url": candidate.photo.url if candidate.photo else "",
            "photo_max_mb": round(settings.CANDIDATE_PHOTO_MAX_UPLOAD_SIZE / (1024 * 1024), 1),
        },
    )


@console_required
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


@console_required
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

@console_required
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
            "upload_form": VoterFileUploadForm(),
            "voter_form": VoterForm(),
            "registration_open": election.state == Election.STATE_REGISTRATION,
        },
    )


@console_required
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


@console_required
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
            _auth, token = issue_authorization(voter, actor=request.user, require_open=False)
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


@console_required
@require_http_methods(["GET", "POST"])
def polling_officer(request, pk):
    """Dedicated polling-station officer token issuance screen."""
    election = _scoped_election(request.user, pk, manage=True)
    # Don't let stale flash messages pop up at the top of this screen.
    list(messages.get_messages(request))
    result = None
    voter = None
    voter_id = ""
    if request.method == "POST":
        voter_id = request.POST.get("voter_id", "").strip()
        if not voter_id:
            result = {"status": "error", "message": "Enter a voter ID."}
        else:
            voter = Voter.objects.filter(
                election=election, voter_id__iexact=voter_id
            ).first()
            from ballot.services import issue_voter_token

            result = issue_voter_token(
                voter, election=election, actor=request.user, request=request
            )
    return render(
        request,
        "elections/polling_officer.html",
        {
            "election": election,
            "voter_id": voter_id,
            "voter": getattr(result, "voter", None) if result else voter,
            "result": result,
        },
    )


@console_required
def voter_registry_index(request):
    """Settings → Voter Registry: pick an election to manage its register."""
    elections = _staff_elections(request.user, manage=True).order_by("-created_at")
    return render(
        request,
        "elections/voter_registry_index.html",
        {"elections": elections},
    )


@console_required
def voter_registry_template(request, pk):
    """Download an example register template (CSV by default, ``?format=xlsx``)."""
    _scoped_election(request.user, pk, manage=True)
    if (request.GET.get("format") or "csv").lower() == "xlsx":
        response = HttpResponse(
            voter_import.template_xlsx_bytes(),
            content_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
        )
        filename = "voter-registry-template.xlsx"
    else:
        response = HttpResponse(
            voter_import.template_csv_bytes(), content_type="text/csv; charset=utf-8"
        )
        filename = "voter-registry-template.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@console_required
@require_http_methods(["POST"])
def voter_file_add(request, pk):
    """Bulk-register voters from an uploaded CSV/TXT/XLSX register file."""
    election = _scoped_election(request.user, pk, manage=True)
    if election.state != Election.STATE_REGISTRATION:
        messages.error(request, "Voters can only be added during REGISTRATION.")
        return redirect("election_voters", pk=election.pk)
    form = VoterFileUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "File upload rejected: " + _form_error_text(form))
        return redirect("election_voters", pk=election.pk)
    try:
        rows = voter_import.rows_from_upload(form.cleaned_data["file"])
    except ValueError as exc:
        messages.error(request, f"File upload rejected: {exc}")
        return redirect("election_voters", pk=election.pk)

    created: list[Voter] = []
    skipped: list[str] = []
    existing: set[str] = set(
        Voter.objects.filter(election=election).values_list("voter_id", flat=True)
    )
    with transaction.atomic():
        for row in rows:
            voter_id = row["voter_id"]
            if voter_id and voter_id in existing:
                skipped.append(voter_id)
                continue
            voter = Voter.objects.create(election=election, **row)
            existing.add(voter.voter_id)
            created.append(voter)
        audit.record(
            action="voters_registered",
            actor=request.user,
            election=election,
            organization=election.organization,
            request=request,
            details={"count": len(created), "source": "file_upload"},
        )

    issued: list[dict] = []
    if form.cleaned_data.get("issue_credentials"):
        for voter in created:
            _auth, token = issue_authorization(voter, actor=request.user, require_open=False)
            issued.append({"voter_id": voter.voter_id, "token": token})

    if issued:
        # Tokens are displayed exactly once, outside the persistent registry.
        return render(
            request,
            "elections/credentials_once.html",
            {"election": election, "issued": issued},
        )
    if skipped:
        preview = ", ".join(skipped[:5])
        messages.warning(
            request,
            f"{len(created)} voters registered from file; "
            f"{len(skipped)} already-registered voter(s) were skipped "
            f"(e.g. {preview}).",
        )
    else:
        messages.success(request, f"{len(created)} voters registered from file.")
    return redirect("election_voters", pk=election.pk)


@console_required
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

@console_required
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

    dual_kind = governance.dual_control_kind(election, election.state, target)
    if dual_kind:
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


@console_required
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


@console_required
@require_http_methods(["POST"])
def approval_decide_view(request, approval_pk):
    """Approve or reject a pending request (four-eyes enforced server-side)."""
    approval = get_object_or_404(ElectionApproval, pk=approval_pk)
    election = _scoped_election(request.user, approval.election_id, manage=True)
    decision = request.POST.get("decision")
    if decision not in ("approve", "reject"):
        # Never infer a decision: a missing/unexpected value must not be read
        # as a rejection (e.g. a submitter button whose value was dropped).
        messages.error(
            request, "No valid decision was submitted; the request is unchanged."
        )
        return redirect("election_config", pk=election.pk)
    approve = decision == "approve"
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
# governance policy + Super Admin emergency override
# ---------------------------------------------------------------------------


@console_required
@require_http_methods(["POST"])
def election_governance(request, pk):
    """Update the per-election governance policy (DRAFT only).

    Restricted to the Super Admin or an election administrator/chair in scope,
    and only while the election is a DRAFT (so the frozen configuration and any
    in-flight process are never silently re-shaped).  The before/after value is
    written to the audit chain.
    """
    election = _scoped_election(request.user, pk, manage=True)
    if not (
        is_system_admin(request.user)
        or can_approve_election(request.user, election)
    ):
        raise PermissionDenied(
            "Only the Super Admin or an election administrator may change the "
            "governance policy."
        )
    if election.state != Election.STATE_DRAFT:
        messages.error(
            request,
            "The governance policy can only be changed while the election is in "
            "DRAFT; it is locked once the election leaves draft.",
        )
        return redirect("election_config", pk=election.pk)

    form = ElectionGovernanceForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Governance policy rejected: " + _form_error_text(form))
        return redirect("election_config", pk=election.pk)

    before = governance.policy_summary(election)
    policy = form.policy()
    rules = dict(election.rules or {})
    rules["governance"] = policy
    election.rules = rules
    election.save(update_fields=["rules"])
    invalidate_config_cache(election)
    after = governance.policy_summary(election)
    audit.record(
        action="governance_policy_updated",
        actor=request.user,
        election=election,
        organization=election.organization,
        request=request,
        object_type="election",
        object_id=str(election.pk),
        details={"before": before, "after": after},
    )
    messages.success(request, "Governance policy updated.")
    return redirect("election_config", pk=election.pk)


@console_required
@require_http_methods(["POST"])
def election_override(request, pk):
    """Super Admin emergency override of a lifecycle transition.

    Explicit, confirmed and fully audited; see
    :func:`elections.lifecycle.admin_override`.  Every other role is refused.
    """
    election = _scoped_election(request.user, pk, manage=True)
    if not is_system_admin(request.user):
        raise PermissionDenied(
            "The emergency override is restricted to the Super Admin."
        )

    form = OverrideForm(request.POST, election=election)
    if not form.is_valid():
        messages.error(request, "Override rejected: " + _form_error_text(form))
        return redirect("election_config", pk=election.pk)

    try:
        election = admin_override(
            election,
            form.cleaned_data["target"],
            actor=request.user,
            reason=form.cleaned_data["reason"],
            confirm=form.cleaned_data["confirm"],
            request=request,
        )
    except LifecycleError as exc:
        messages.error(request, exc.message)
        return redirect("election_config", pk=election.pk)

    messages.success(
        request,
        f"Emergency override applied; the election is now {election.state.upper()}. "
        "The override is recorded in the audit chain.",
    )
    return redirect("election_config", pk=election.pk)


@console_required
@require_http_methods(["GET", "POST"])
def election_delete(request, pk):
    """Super Admin permanent election deletion with explicit confirmation."""
    election = _scoped_election(request.user, pk, manage=True)
    if not is_system_admin(request.user):
        raise PermissionDenied("The emergency deletion is restricted to the Super Admin.")

    if request.method == "POST":
        form = ElectionDeleteForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Deletion rejected: " + _form_error_text(form))
            return redirect("election_config", pk=election.pk)

        reason = form.cleaned_data["reason"]
        confirm = form.cleaned_data["confirm"]

        from django.db import transaction
        from django.conf import settings

        election_id = election.id
        election_title = election.title
        chain_key = f"election:{election_id}"
        organization = election.organization

        with transaction.atomic():
            # Lock election and related key rows
            locked = Election.objects.select_for_update().get(pk=election_id)
            # Clear in-memory related objects by deleting in dependency order
            # Delete in order to respect constraints deliberately
            from representatives.models import AttendanceRecord, IncidentReport, Representative
            from tally.models import TallySnapshot
            from ballot.models import Ballot, BallotAuthorization, BallotPreparation, BallotBox

            BallotPreparation.objects.filter(election=locked).delete()
            Ballot.objects.filter(election=locked).delete()
            BallotAuthorization.objects.filter(election=locked).delete()
            BallotBox.objects.filter(election=locked).delete()

            TallySnapshot.objects.filter(election=locked).delete()
            AttendanceRecord.objects.filter(representative__election=locked).delete()
            IncidentReport.objects.filter(election=locked).delete()
            Representative.objects.filter(election=locked).delete()

            # Delete election approvals, polling stations, positions, voters, candidates
            locked.approvals.all().delete()
            locked.stations.all().delete()
            locked.positions.all().delete()
            locked.voters.all().delete()

            # Record audit event for deletion (before deleting row so we have context)
            audit.record(
                action="election_deleted",
                actor=request.user,
                election=locked,
                organization=organization,
                request=request,
                object_type="election",
                object_id=str(election_id),
                reason=reason,
                details={
                    "title": election_title,
                    "deleted": True,
                    "scope": "system-wide (Super Admin)",
                    "consequences": "Permanent deletion of election and related records (Super Admin override).",
                },
            )
            # Delete the election row
            locked.delete()

        messages.success(
            request,
            f"Election '{election_title}' permanently deleted. The audit chain for {chain_key} remains intact for verification.",
        )
        return redirect("dashboard")

    ctx = {
        "election": election,
        "delete_form": ElectionDeleteForm(),
    }
    return render(request, "elections/election_delete_confirm.html", ctx)


# ---------------------------------------------------------------------------
# audit trail surface
# ---------------------------------------------------------------------------

@console_required
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


@console_required
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


# ---------------------------------------------------------------------------
# cross-election console
# ---------------------------------------------------------------------------
#
# These surfaces answer, for a whole tenant at once: what needs attention, what
# is waiting on me, what has been certified, and whether the audit chains are
# intact.  All of them are scoped through ``_staff_elections`` so no user ever
# sees an election outside their organization subtree.

@console_required
def operations(request):
    """'Needs attention': every blocking/warned election plus pending approvals.

    Ordered so elections with blocking readiness problems come first, then
    elections awaiting a decision, then advisory warnings.  Read-only: every
    action is carried out on the election workspace itself.
    """
    elections = list(_staff_elections(request.user).order_by("-created_at"))
    is_super = is_system_admin(request.user)
    items: list[dict] = []
    for election in elections:
        report = readiness.assess(election)
        pending = list(
            election.approvals.filter(status=ElectionApproval.STATUS_PENDING).select_related(
                "requested_by"
            )
        )
        can_approve = can_approve_election(request.user, election)
        decidable = [
            a
            for a in pending
            if can_approve and (is_super or a.requested_by_id != request.user.pk)
        ]
        if report["attention"] or pending:
            items.append(
                {
                    "election": election,
                    "report": report,
                    "blocking": report["blocking"],
                    "warnings": report["warnings"],
                    "decidable": decidable,
                    "pending_count": len(pending),
                    "can_manage": can_manage_election(request.user, election),
                }
            )
    items.sort(key=lambda row: (not row["blocking"], not row["pending_count"], row["election"].title.lower()))
    return render(
        request,
        "elections/operations.html",
        {
            "items": items,
            "election_total": len(elections),
            "attention_total": len(items),
            "blocking_total": sum(1 for row in items if row["blocking"]),
        },
    )


@console_required
def approvals_inbox(request):
    """Every four-eyes request relevant to the user, across all their elections.

    Administrators see the requests they may decide (never their own); officials
    see the requests they raised and their status.  Four-eyes is enforced again
    server-side in ``decide_approval`` - this page only mirrors it.
    """
    elections = list(_staff_elections(request.user).order_by("-created_at"))
    manage_ids = {e.pk for e in elections if can_manage_election(request.user, e)}
    approve_ids = {e.pk for e in elections if can_approve_election(request.user, e)}
    if not manage_ids and not approve_ids:
        raise PermissionDenied(
            "The approvals inbox is available to election officials and administrators."
        )

    pending = list(
        ElectionApproval.objects.filter(
            status=ElectionApproval.STATUS_PENDING,
            election_id__in=manage_ids | approve_ids,
        )
        .select_related("election", "requested_by", "election__organization")
        .order_by("requested_at")
    )
    is_super = is_system_admin(request.user)
    decidable = [
        a
        for a in pending
        if a.election_id in approve_ids and (is_super or a.requested_by_id != request.user.pk)
    ]
    awaiting = [a for a in pending if a.requested_by_id == request.user.pk]
    decided = list(
        ElectionApproval.objects.filter(
            election_id__in=manage_ids | approve_ids,
        )
        .exclude(status=ElectionApproval.STATUS_PENDING)
        .select_related("election", "requested_by", "decided_by")
        .order_by("-decided_at")[:15]
    )
    return render(
        request,
        "elections/approvals_inbox.html",
        {
            "decidable": decidable,
            "awaiting": awaiting,
            "decided": decided,
            "can_approve_any": bool(approve_ids),
        },
    )


@console_required
def results_index(request):
    """Cross-election results & reports index (latest snapshot per election)."""
    from tally.services import latest_snapshot

    elections = [
        e
        for e in _staff_elections(request.user).order_by("-created_at")
        if can_view_results_early(request.user, e)
    ]
    rows = []
    for election in elections:
        snapshot = latest_snapshot(election)
        rows.append(
            {
                "election": election,
                "snapshot": snapshot,
                "has_result": snapshot is not None,
                "published": election.state
                in (Election.STATE_PUBLISHED, Election.STATE_ARCHIVED),
            }
        )
    return render(
        request,
        "elections/results_index.html",
        {
            "rows": rows,
            "certified_total": sum(
                1 for r in rows if r["snapshot"] and r["snapshot"].status == "certified"
            ),
            "published_total": sum(1 for r in rows if r["published"]),
        },
    )


@console_required
def audit_overview(request):
    """Tenant-wide audit view: chain integrity and checkpoints per election."""
    from audit.models import AuditCheckpoint, AuditEvent

    elections = list(_staff_elections(request.user).order_by("-created_at")[:100])
    rows = []
    for election in elections:
        key = f"election:{election.pk}"
        rows.append(
            {
                "election": election,
                "chain": audit.verify_chain(key),
                "last_event": AuditEvent.objects.filter(election=election)
                .order_by("-seq")
                .first(),
                "checkpoints": AuditCheckpoint.objects.filter(chain_key=key).count(),
            }
        )
    ok_total = sum(1 for r in rows if r["chain"]["ok"])
    return render(
        request,
        "elections/audit_overview.html",
        {
            "rows": rows,
            "ok_total": ok_total,
            "broken_total": len(rows) - ok_total,
        },
    )


@console_required
def settings_overview(request):
    """Read-only summary of the deployment's security-relevant configuration.

    Deliberately never renders secret keys or pepper values - only whether a
    setting is configured and which algorithms/limits are in force, so an
    administrator can confirm the platform is correctly hardened.
    """
    if not is_system_admin(request.user):
        raise PermissionDenied("System settings are restricted to system administrators.")

    from django import get_version
    from django.conf import settings

    from audit import keys

    def configured(value) -> bool:
        return bool(value) if value is not None else False

    config = {
        "django_version": get_version(),
        "debug": settings.DEBUG,
        "time_zone": settings.TIME_ZONE,
        "language": settings.LANGUAGE_CODE,
        "session_age_hours": round(settings.SESSION_COOKIE_AGE / 3600, 1),
        "session_browser_close": settings.SESSION_EXPIRE_AT_BROWSER_CLOSE,
        "session_httponly": settings.SESSION_COOKIE_HTTPONLY,
        "csrf_samesite": settings.CSRF_COOKIE_SAMESITE,
        "x_frame_options": getattr(settings, "X_FRAME_OPTIONS", ""),
        "content_type_nosniff": getattr(settings, "SECURE_CONTENT_TYPE_NOSNIFF", False),
        "referrer_policy": getattr(settings, "SECURE_REFERRER_POLICY", ""),
        "signing_algorithm": keys.algorithm(),
        "ballot_key_configured": configured(getattr(settings, "BALLOT_ENCRYPTION_KEY", ""))
        or getattr(settings, "BALLOT_KEY_FILE", None)
        and settings.BALLOT_KEY_FILE.exists(),
        "result_key_configured": configured(getattr(settings, "RESULT_SIGNING_KEY", ""))
        or getattr(settings, "RESULT_SIGNING_KEY_FILE", None)
        and settings.RESULT_SIGNING_KEY_FILE.exists(),
        "token_pepper_configured": configured(getattr(settings, "VOTER_TOKEN_PEPPER", "")),
        "celery_eager": getattr(settings, "CELERY_TASK_ALWAYS_EAGER", True),
        "rate_limits": {
            "window_seconds": getattr(settings, "RATE_LIMIT_WINDOW", None),
            "voter_login": getattr(settings, "RATE_LIMIT_VOTER_LOGIN", None),
            "ballot_cast": getattr(settings, "RATE_LIMIT_BALLOT_CAST", None),
            "report_download": getattr(settings, "RATE_LIMIT_REPORT_DOWNLOAD", None),
        },
    }

    from django.contrib.auth import get_user_model

    from organizations.models import Organization, OrganizationMembership

    counts = {
        "organizations": Organization.objects.count(),
        "active_organizations": Organization.objects.filter(is_active=True).count(),
        "users": get_user_model().objects.count(),
        "staff_users": get_user_model().objects.filter(is_staff=True).count(),
        "memberships": OrganizationMembership.objects.count(),
        "elections": Election.objects.count(),
    }
    return render(
        request,
        "elections/settings_overview.html",
        {"config": config, "counts": counts},
    )

from .workstation_views import *  # noqa: F401,F403

