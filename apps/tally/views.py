"""Tally + results views. Visibility enforced by policy + role, server-side."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.core.permissions import is_org_official, user_orgs
from apps.elections.models import Election
from .engine import calculate_tally, verify_tally
from .models import PositionResult, ReceiptRegister, ResultVersion


def _scoped_election(request, slug) -> Election:
    qs = Election.objects.select_related("organization")
    if not request.user.is_superuser:
        org_ids = user_orgs(request.user).values_list("organization_id", flat=True)
        qs = qs.filter(organization_id__in=org_ids)
    return get_object_or_404(qs, slug=slug)


@login_required
@never_cache
def results(request, slug):
    """Results page — official/auditor view of all versions."""
    election = _scoped_election(request, slug)
    if not is_org_official(request.user, election.organization):
        raise PermissionDenied
    versions = election.result_versions.select_related(
        "calculated_by", "certified_by", "supersedes"
    )
    version_no = request.GET.get("v")
    current = None
    if version_no and version_no.isdigit():
        current = versions.filter(version_no=int(version_no)).first()
    if current is None:
        current = versions.first()
    position_results = []
    if current:
        position_results = list(
            current.position_results.select_related("position").order_by(
                "position__order"
            )
        )
        for pr in position_results:
            pr.ranked = sorted(pr.votes_for.items(), key=lambda kv: (-kv[1], kv[0]))
    return render(
        request,
        "tally/results.html",
        {
            "election": election,
            "versions": versions,
            "current": current,
            "position_results": position_results,
            "is_official": True,
        },
    )


@login_required
@require_http_methods(["POST"])
def run_tally(request, slug):
    election = _scoped_election(request, slug)
    if not is_org_official(request.user, election.organization):
        raise PermissionDenied
    reason = request.POST.get("reason", "")
    supersedes = None
    if reason:
        supersedes = election.result_versions.order_by("-version_no").first()
    try:
        result = calculate_tally(
            election, request.user, recount_reason=reason, supersedes=supersedes
        )
        messages.success(
            request,
            f"Tally v{result.version_no} calculated "
            f"({result.ballots_counted} counted / {result.ballots_accepted} accepted) "
            f"— reconciliation {'OK' if result.reconciled else 'MISMATCH'}.",
        )
    except Exception as exc:
        messages.error(request, f"Tally failed: {exc}")
    return redirect("tally:results", slug=slug)


@login_required
@require_http_methods(["POST"])
def certify(request, slug):
    """Certification goes through the state machine (separation of duties)."""
    from apps.elections import state_machine
    from apps.elections.state_machine import StateTransitionError

    election = _scoped_election(request, slug)
    try:
        state_machine.transition(
            election, Election.State.CERTIFIED, request.user,
            reason=request.POST.get("reason", "certification"),
        )
        messages.success(request, "Results certified.")
    except (StateTransitionError, PermissionDenied, ValidationError) as exc:
        msgs = getattr(exc, "messages", None) or [str(exc)]
        for m in msgs:
            messages.error(request, m)
    return redirect("tally:results", slug=slug)


@login_required
@never_cache
def verify(request, slug):
    """Independent verification view: recompute tally hash, check audit chain."""
    from apps.audit.services import verify_chain

    election = _scoped_election(request, slug)
    if not is_org_official(request.user, election.organization):
        raise PermissionDenied
    checks = []
    for rv in election.result_versions.all():
        if rv.status in (ResultVersion.Status.CALCULATED, ResultVersion.Status.CERTIFIED,
                         ResultVersion.Status.PUBLISHED):
            checks.append({"version": rv, "verify": verify_tally(rv)})
    chain = verify_chain(organization=election.organization)
    grants = election.ballot_grants.filter(status="consumed").count()
    accepted = election.accepted_ballots.count()
    return render(
        request,
        "tally/verify.html",
        {
            "election": election,
            "checks": checks,
            "chain": chain,
            "reconciliation": {
                "grants_consumed": grants,
                "ballots_accepted": accepted,
                "match": grants == accepted,
            },
        },
    )


@login_required
def receipt_register(request, slug):
    election = _scoped_election(request, slug)
    if not is_org_official(request.user, election.organization):
        raise PermissionDenied
    receipts = ReceiptRegister.objects.filter(election=election)[:500]
    return render(
        request,
        "tally/receipt_register.html",
        {"election": election, "receipts": receipts},
    )


# ---------------------------------------------------------------------------
# Public results (published only)
# ---------------------------------------------------------------------------
@never_cache
def public_results(request, slug):
    election = get_object_or_404(Election, slug=slug, organization__is_active=True)
    if election.state not in (Election.State.PUBLISHED, Election.State.ARCHIVED):
        # Same message for draft vs certified-not-published (no state leak to
        # anonymous users beyond "not published").
        return render(
            request,
            "tally/public_pending.html",
            {"election": election},
        )
    current = election.result_versions.order_by("-version_no").first()
    position_results = []
    if current:
        position_results = list(
            current.position_results.select_related("position").order_by("position__order")
        )
        for pr in position_results:
            pr.ranked = sorted(pr.votes_for.items(), key=lambda kv: (-kv[1], kv[0]))
    return render(
        request,
        "tally/public_results.html",
        {
            "election": election,
            "current": current,
            "position_results": position_results,
        },
    )
