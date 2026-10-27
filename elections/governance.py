"""Per-election governance policy.

Institutions can shape two things per election, stored in the existing
``Election.rules`` JSON field (therefore no schema migration is required):

* **Who may certify / publish** results (``certify_roles`` / ``publish_roles``).
  By default any manager (officials, chairs, administrators) may do so, which
  preserves the historical behaviour.  An election can narrow this to
  institution administrators and/or the election commission chair.
* **Whether the four-eyes (dual-control) procedure is required** for the
  sensitive transitions (``approve_config``, ``reopen``, ``decertify``).  It is
  on by default; an institution may document a lighter procedure.

The policy is *data*, not code: it is read server-side from the election row.
It never changes the config hash (``elections.config`` does not hash ``rules``)
and it is never taken from request parameters.  Changing it is restricted to a
draft election (see ``elections.views.election_governance``) and is audited with
the before/after value.

The Super Admin is never bound by this policy for the purpose of *completing*
work: they can always use the emergency override, and self-decide their own
approval requests (``elections.lifecycle``).
"""

from __future__ import annotations

from organizations import roles

from .models import Election

# UI choice -> the role codes that satisfy it.
CERTIFY_AUTHORITY_CHOICES = [
    ("manage", "Election officials, chairs & administrators"),
    ("admins", "Chair & institution administrators"),
    ("org_admin", "Institution election administrators only"),
    ("ec_chair", "Election commission chair only"),
]

_AUTHORITY_ROLES: dict[str, tuple[str, ...]] = {
    "manage": roles.MANAGE_ROLES,
    "admins": (roles.ORG_ADMIN, roles.EC_CHAIR),
    "org_admin": (roles.ORG_ADMIN,),
    "ec_chair": (roles.EC_CHAIR,),
}

DUAL_KINDS = ("approve_config", "reopen", "decertify")

# Edge -> approval kind.  The canonical map lives here so both the lifecycle
# enforcement and the per-election policy consult the same source.
DUAL_CONTROL_EDGES: dict[tuple[str, str], str] = {
    (Election.STATE_CONFIG_REVIEW, Election.STATE_APPROVED): "approve_config",
    (Election.STATE_CLOSED, Election.STATE_OPEN): "reopen",
    (Election.STATE_CERTIFIED, Election.STATE_REVIEW): "decertify",
    (Election.STATE_PUBLISHED, Election.STATE_REVIEW): "decertify",
}


def raw_policy(election) -> dict:
    rules = getattr(election, "rules", None) or {}
    value = rules.get("governance")
    return value if isinstance(value, dict) else {}


def authority_roles(value) -> tuple[str, ...] | None:
    """Translate a stored authority value to role codes (``None`` = default)."""
    if not value:
        return None
    return _AUTHORITY_ROLES.get(str(value))


def policy_for(election) -> dict:
    """Normalised governance policy for an election."""
    raw = raw_policy(election)
    require_institution_approval = raw.get("require_institution_approval", True)
    stored_dual = raw.get("dual_control")
    if not isinstance(stored_dual, dict):
        stored_dual = {}
    dual = {}
    for kind in DUAL_KINDS:
        value = stored_dual.get(kind, True)
        dual[kind] = bool(require_institution_approval) and bool(value)
    return {
        "certify_authority": raw.get("certify_authority") or "manage",
        "publish_authority": raw.get("publish_authority") or "manage",
        "certify_roles": authority_roles(raw.get("certify_authority")),
        "publish_roles": authority_roles(raw.get("publish_authority")),
        "require_institution_approval": bool(require_institution_approval),
        "dual_control": dual,
    }


def dual_control_kind(election, src: str, dst: str) -> str | None:
    """The approval kind required for this edge, or ``None`` if direct."""
    kind = DUAL_CONTROL_EDGES.get((src, dst))
    if kind is None:
        return None
    if not policy_for(election)["dual_control"].get(kind, True):
        return None
    return kind


def _allowed_by(value, user, election) -> bool:
    if roles.is_system_admin(user):
        return True
    role_codes = authority_roles(value)
    if role_codes is None:
        return roles.can_manage_election(user, election)
    return roles.role_in(user, election.organization, role_codes)


def can_certify(user, election) -> bool:
    return _allowed_by(policy_for(election)["certify_authority"], user, election)


def can_publish(user, election) -> bool:
    return _allowed_by(policy_for(election)["publish_authority"], user, election)


def authority_label(value) -> str:
    return dict(CERTIFY_AUTHORITY_CHOICES).get(str(value or "manage"), "Election officials, chairs & administrators")


def policy_summary(election) -> dict:
    """Human-readable policy summary for the workspace/chair dashboard."""
    policy = policy_for(election)
    return {
        "certify_authority": policy["certify_authority"],
        "certify_label": authority_label(policy["certify_authority"]),
        "publish_authority": policy["publish_authority"],
        "publish_label": authority_label(policy["publish_authority"]),
        "require_institution_approval": policy["require_institution_approval"],
        "dual_control": policy["dual_control"],
    }
