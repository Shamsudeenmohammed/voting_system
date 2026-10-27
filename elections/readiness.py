"""Election readiness: "what is missing, who acts, what next".

Pure, read-only helper used by the election workspace, the cross-election
*Needs attention* console and the dashboards.  It never changes state - it
only explains the current state so an operator is never left guessing why a
transition button is refused.  The authoritative rules remain in
:mod:`elections.lifecycle`; this module is a mirror, not an enforcer.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Election

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    ok: bool
    detail: str
    severity: str = ERROR
    responsible: str = "Election official"


# State -> (action, label, who) for the single most useful next step.
_NEXT_ACTION = {
    Election.STATE_DRAFT: ("submit", "Open registration", "Election official"),
    Election.STATE_REGISTRATION: (
        "request_review",
        "Submit configuration for review",
        "Election official",
    ),
    Election.STATE_CONFIG_REVIEW: (
        "approve_config",
        "Request configuration approval",
        "Organization administrator",
    ),
    Election.STATE_APPROVED: ("schedule", "Schedule the election", "Election official"),
    Election.STATE_SCHEDULED: ("open", "Open voting (or wait for the start time)", "Election official"),
    Election.STATE_OPEN: ("close", "Close voting", "Election official"),
    Election.STATE_CLOSED: ("start_reconciliation", "Start reconciliation", "Election official"),
    Election.STATE_RECONCILIATION: (
        "complete_reconciliation",
        "Submit results for review",
        "Election official",
    ),
    Election.STATE_REVIEW: ("certify", "Certify the results", "Organization administrator"),
    Election.STATE_CERTIFIED: ("publish", "Publish the results", "Election official"),
    Election.STATE_PUBLISHED: ("archive", "Archive the election", "Election official"),
}


def _checks(election: Election) -> list[Check]:
    checks: list[Check] = []

    # 1. Rules & schedule ---------------------------------------------------
    schedule_ok = election.end_at > election.start_at
    checks.append(
        Check(
            key="rules",
            label="Rules & schedule",
            ok=bool(election.title) and schedule_ok,
            detail=(
                "Voting window is well defined."
                if schedule_ok
                else "The closing time must be after the opening time."
            ),
        )
    )

    # 2. Ballot structure ---------------------------------------------------
    positions = list(election.positions.prefetch_related("candidates"))
    checks.append(
        Check(
            key="positions",
            label="Positions defined",
            ok=bool(positions),
            detail=(
                f"{len(positions)} position(s) on the ballot."
                if positions
                else "Add at least one position."
            ),
        )
    )

    # 3. Candidates ---------------------------------------------------------
    missing = [
        p.title for p in positions if not any(c.status == "approved" for c in p.candidates.all())
    ]
    checks.append(
        Check(
            key="candidates",
            label="Every position has an approved candidate",
            ok=bool(positions) and not missing,
            detail=(
                "All positions are contestable."
                if positions and not missing
                else (
                    "Add an approved candidate to: " + ", ".join(missing)
                    if missing
                    else "Define positions first."
                )
            ),
        )
    )

    # 4. Passport photographs (advisory, or blocking if the election rules
    #    explicitly require them) -------------------------------------------
    approved_candidates = [
        c for p in positions for c in p.candidates.all() if c.status == "approved"
    ]
    photos_missing = [c.full_name for c in approved_candidates if not c.photo]
    photos_required = bool(election.rules.get("require_candidate_photos"))
    checks.append(
        Check(
            key="candidate_photos",
            label="Approved candidates have a passport photograph",
            ok=not photos_missing,
            detail=(
                f"All {len(approved_candidates)} approved candidate(s) have a photograph."
                if approved_candidates and not photos_missing
                else (
                    "Missing a passport photograph for: " + ", ".join(photos_missing)
                    if photos_missing
                    else "No approved candidates yet."
                )
            ),
            severity=ERROR if photos_required else WARNING,
            responsible="Election official",
        )
    )

    # 5. Registry (advisory) ------------------------------------------------
    active_voters = election.voters.filter(status="active").count()
    checks.append(
        Check(
            key="registry",
            label="Voter registry populated",
            ok=active_voters > 0,
            detail=(
                f"{active_voters} active voter(s) registered."
                if active_voters
                else "No active voters yet — register them during REGISTRATION."
            ),
            severity=WARNING,
        )
    )

    # 6. Polling stations (advisory) ---------------------------------------
    station_count = election.stations.count()
    checks.append(
        Check(
            key="stations",
            label="Polling stations configured",
            ok=station_count > 0,
            detail=(
                f"{station_count} station(s) configured."
                if station_count
                else "Optional for online-only elections, but needed for on-site voting and per-station monitoring."
            ),
            severity=WARNING,
        )
    )

    return checks


def assess(election: Election) -> dict:
    """Return a readiness report for one election."""
    checks = _checks(election)
    blocking = [c for c in checks if not c.ok and c.severity == ERROR]
    warnings = [c for c in checks if not c.ok and c.severity == WARNING]

    next_action = None
    if election.state != Election.STATE_ARCHIVED:
        action, label, who = _NEXT_ACTION.get(
            election.state, ("", "Review the election", "Election official")
        )
        # If setup is incomplete, that is the real next step regardless of state.
        if blocking and election.state in (
            Election.STATE_DRAFT,
            Election.STATE_REGISTRATION,
        ):
            next_action = {
                "action": "fix_setup",
                "label": "Complete the ballot setup",
                "who": "Election official",
            }
        elif action:
            next_action = {"action": action, "label": label, "who": who}

    return {
        "checks": checks,
        "blocking": blocking,
        "warnings": warnings,
        "ready": not blocking,
        "next_action": next_action,
        "attention": bool(blocking) or bool(warnings),
    }
