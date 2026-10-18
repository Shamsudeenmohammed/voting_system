"""Eligibility evaluation — deterministic, testable, org-scoped."""
from __future__ import annotations

from django.db.models import Q
from django.utils import timezone

from apps.elections.models import Election
from .models import EligibilityRule, Voter


def eligibility_qs(election: Election):
    """Queryset of voters eligible for ``election`` (same org + rules)."""
    qs = Voter.objects.filter(
        organization_id=election.organization_id,
        status=Voter.Status.ACTIVE,
    )
    rules = list(
        eligibility_rules_for(election)
    )
    if not rules:
        return qs
    combined = Q()
    for rule in rules:
        if not rule.is_active:
            continue
        if rule.match == EligibilityRule.Match.UNIT and rule.unit_id:
            combined &= Q(unit_id=rule.unit_id) | Q(units=rule.unit_id)
        elif rule.match == EligibilityRule.Match.UNIT_TYPE and rule.unit_type:
            combined &= Q(unit__unit_type=rule.unit_type)
        elif rule.match == EligibilityRule.Match.VERIFIED:
            combined &= Q(is_verified=True)
        elif rule.match == EligibilityRule.Match.STATUS and rule.status:
            combined &= Q(status=rule.status)
    return qs.filter(combined).distinct()


def eligibility_rules_for(election: Election):
    return list(election.eligibility_rules.filter(is_active=True))


def is_eligible(election: Election, voter: Voter) -> tuple[bool, str]:
    """Return (eligible, reason-code). Reason codes are non-sensitive."""
    if voter.organization_id != election.organization_id:
        return False, "wrong_org"
    if voter.status != Voter.Status.ACTIVE:
        return False, f"status_{voter.status}"
    if election.state not in (
        Election.State.REGISTRATION,
        Election.State.CONFIGURATION_REVIEW,
        Election.State.APPROVED,
        Election.State.SCHEDULED,
        Election.State.OPEN,
    ):
        return False, "registration_closed"
    if election.registration_closes and timezone.now() > election.registration_closes:
        # Registration-phase cutoff; once open, voters on the frozen list vote.
        if election.state == Election.State.REGISTRATION:
            return False, "registration_closed"
    rules = eligibility_rules_for(election)
    if not rules:
        return True, "ok"
    ok = eligibility_qs(election).filter(pk=voter.pk).exists()
    return (True, "ok") if ok else (False, "rules_not_matched")


def eligible_count(election: Election) -> int:
    return eligibility_qs(election).count()
