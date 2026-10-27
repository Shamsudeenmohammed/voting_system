"""Task-oriented, role-aware navigation.

Builds the sidebar model for the current request.  The navigation is derived
from the user's *real* permissions (organization memberships plus the
``is_staff`` / ``is_superuser`` flags, and the explicit system-wide chair
grant) so that no link is shown to a user who would be refused by the server.
Groups and items are plain dictionaries so templates stay logic-free.

Security note: this module only decides what is *displayed*.  Every view keeps
its own organization-scoped permission checks; hiding a link is never the
enforcement mechanism.
"""
from __future__ import annotations

from django.urls import NoReverseMatch, reverse

from . import roles
from .roles import memberships


# Group/item order and icons.  Only items whose predicate passes are emitted.
def _item(label: str, url_name: str, icon: str, *, arg=None, badge: str = "") -> dict:
    try:
        url = reverse(url_name, args=[arg] if arg else None)
    except NoReverseMatch:  # pragma: no cover - misconfiguration guard
        return {}
    return {"label": label, "url_name": url_name, "url": url, "icon": icon, "badge": badge}


def _roles_for(user) -> set[str]:
    return {m.role for m in memberships(user)}


def _approved_representative(user):
    try:
        from representatives.models import Representative
    except Exception:  # pragma: no cover - import guard
        return None
    return (
        Representative.objects.filter(
            user=user, status=Representative.STATUS_APPROVED
        )
        .select_related("election")
        .first()
    )


def build_navigation(request) -> list[dict]:
    """Return the ordered list of navigation groups for ``request.user``."""
    user = getattr(request, "user", None)
    # Voting kiosks must never render administrative navigation.
    try:
        from elections.workstation_auth import get_workstation
        from elections.models import Workstation

        ws = get_workstation(request)
        if ws is not None and ws.purpose == Workstation.PURPOSE_VOTING:
            return []
    except Exception:
        pass
    if user is None or not user.is_authenticated:
        return []

    admin = roles.is_system_admin(user)
    system_wide = roles.has_system_wide_scope(user)
    role_set = _roles_for(user)
    console_roles = set(roles.CONSOLE_ROLES)

    manage = admin or system_wide or bool(role_set & set(roles.MANAGE_ROLES))
    read = admin or system_wide or bool(role_set & console_roles)
    chair = admin or system_wide or roles.EC_CHAIR in role_set
    can_admin_orgs = roles.can_administer_organizations(user)
    can_manage_people = roles.can_manage_officials(user)
    console = admin or bool(getattr(user, "is_staff", False)) or bool(role_set & console_roles)
    rep = None if console else _approved_representative(user)

    # A user who is neither console staff nor an approved representative gets
    # no sidebar (they still see the header / public pages).
    if not console and rep is None:
        return []

    groups: list[dict] = []

    def add(key: str, label: str, icon: str, items: list[dict]) -> None:
        items = [i for i in items if i]
        if items:
            groups.append({"key": key, "label": label, "icon": icon, "items": items})

    # ------------------------------------------------ Administration and Election Setup
    admin_setup_items = []
    if console:
        admin_setup_items.append(_item("Dashboard", "dashboard", "bi-speedometer2"))
    if chair:
        admin_setup_items.append(_item("Election oversight", "chair_dashboard", "bi-clipboard-check"))
    if admin:
        admin_setup_items.extend([
            _item("Control centre", "control_center", "bi-speedometer"),
            _item("System search", "control_center_search", "bi-search"),
            _item("Overrides log", "control_center_overrides", "bi-exclamation-octagon"),
            _item("Recovery", "control_center_recovery", "bi-life-preserver"),
            _item("System settings", "settings_overview", "bi-sliders2"),
            _item("Voter Registry", "voter_registry_index", "bi-person-vcard"),
        ])
    if can_admin_orgs:
        admin_setup_items.extend([
            _item("Organisations", "manage_organizations", "bi-buildings"),
            _item("People & access", "manage_people", "bi-people"),
        ])
    elif can_manage_people:
        admin_setup_items.append(_item("People & access", "manage_people", "bi-people"))
    if manage:
        admin_setup_items.append(_item("New election", "election_create", "bi-plus-circle"))
    if manage:
        admin_setup_items.append(_item("Workstations", "workstation-manage", "bi-pc-display"))
    if rep is not None:
        admin_setup_items.append(_item("Browse elections", "home", "bi-collection"))
    add("admin_setup", "Administration and Election Setup", "bi-gear", admin_setup_items)

    # ------------------------------------------------ Voter Verification and Token Issuance
    verification_items = []
    if console:
        verification_items.append(_item("Verification console", "workstation-verification", "bi-person-check"))
        # One shared device sign-in for both verification and voting kiosks.
        verification_items.append(_item("Workstation login", "workstation-login", "bi-box-arrow-in-right"))
    add("verification", "Voter Verification and Token Issuance", "bi-person-badge", verification_items)

    # ------------------------------------------------ Polling-Station Voting
    voting_items = []
    # ``/kiosk/`` is a compatibility alias for this same view.  When a device is
    # not signed in yet the view redirects to the shared workstation login, so
    # the sidebar only needs the one canonical kiosk entry here.
    voting_items.append(_item("Voting kiosk", "workstation-voting", "bi-display"))
    add("voting", "Polling-Station Voting", "bi-check2-square", voting_items)

    # ------------------------------------------------ Engine Room Monitoring
    engine_items = []
    if rep is not None:
        engine_items.append(_item("My monitoring", "representative_dashboard", "bi-eye"))
    if rep is not None and rep.election_id:
        engine_items.append(_item("My live monitor", "monitor", "bi-broadcast", arg=rep.election_id))
    if (
        rep is not None
        and rep.election_id
        and roles.can_view_live_totals(user, rep.election)
    ):
        engine_items.append(
            _item("Live results board", "live_board", "bi-easel2", arg=rep.election_id)
        )
    add("engine_room", "Engine Room Monitoring", "bi-eye", engine_items)

    # ------------------------------------------------ Results, Reconciliation and Audit
    results_audit_items = []
    if read:
        results_audit_items.append(_item("Needs attention", "operations", "bi-clipboard-check"))
    if manage:
        results_audit_items.append(_item("Approvals", "approvals_inbox", "bi-shield-check"))
    if read:
        results_audit_items.append(_item("Results & reports", "results_index", "bi-file-earmark-bar-graph"))
    if read:
        results_audit_items.append(_item("Audit overview", "audit_overview", "bi-link-45deg"))
    add("results_audit", "Results, Reconciliation and Audit", "bi-bar-chart", results_audit_items)

    _mark_active(request, groups)
    return groups


def _mark_active(request, groups: list[dict]) -> None:
    """Highlight the single best-matching item (longest URL prefix)."""
    path = getattr(request, "path", "") or ""
    best = None
    best_len = -1
    for group in groups:
        group["active"] = False
        group["open"] = False
        for item in group["items"]:
            item["active"] = False
            url = item["url"]
            if url == "/":
                matches = path == "/"
                length = 1
            else:
                matches = path == url or path.startswith(url)
                length = len(url)
            if matches and length > best_len:
                best = (group, item)
                best_len = length
    if best is not None:
        group, item = best
        item["active"] = True
        group["active"] = True
        group["open"] = True
