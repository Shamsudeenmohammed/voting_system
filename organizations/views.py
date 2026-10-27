"""Organization and people-access console.

These views are the *product* surface for tenancy administration, replacing
the Django shell/ORM as the only way to create organizations and assign role
memberships.  They add no permissions of their own: every list is scoped to
``organizations.roles`` predicates and every write is re-checked against that
scope, so an ``org_admin`` (or election-commission chair) can never widen
access beyond their own tenant subtree.

Two distinct capabilities are enforced here:

* **Organization lifecycle** (create/rename/activate tenants) - the Super Admin
  and ``org_admin`` only (:func:`organizations.roles.can_administer_organizations`).
* **People & access** (appoint/replace officials) - the Super Admin,
  ``org_admin`` and the election-commission chair
  (:func:`organizations.roles.can_manage_officials`).  A chair may only grant or
  revoke ``election_official`` and ``observer`` roles, never another
  administrator or chair (no privilege escalation).

``is_staff`` is kept in sync with console-role membership so the legacy admin
back-office and the product console stay aligned; it is never the enforcement
mechanism (see :func:`organizations.roles.console_required`).
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from audit import services as audit

from .forms import MembershipGrantForm, OrganizationForm, UserCreateForm
from .models import Organization, OrganizationMembership
from .roles import (
    ALL_ROLES,
    CHAIR_GRANTABLE_ROLES,
    CONSOLE_ROLES,
    admin_organizations_for,
    can_administer_organizations,
    can_manage_officials,
    grantable_roles,
    has_system_wide_scope,
    is_system_admin,
    people_organizations_for,
)

User = get_user_model()

ROLE_LABELS = dict(OrganizationMembership.ROLE_CHOICES)


def _manage_people_gate(request) -> None:
    if not can_manage_officials(request.user):
        raise PermissionDenied(
            "Managing people and access requires institution-administrator or "
            "election-commission-chair rights in at least one organization."
        )


def _grantable_choices(user) -> list[str]:
    """Role codes the user may grant anywhere in their people scope."""
    if is_system_admin(user) or has_system_wide_scope(user):
        return list(ALL_ROLES)
    if admin_organizations_for(user).exists():
        return list(ALL_ROLES)
    return list(CHAIR_GRANTABLE_ROLES)


def _sync_staff_flag(user) -> None:
    """Keep ``is_staff`` aligned with console-role membership."""
    if user.is_superuser:
        return
    should_be_staff = user.org_memberships.filter(role__in=CONSOLE_ROLES).exists()
    if user.is_staff != should_be_staff:
        user.is_staff = should_be_staff
        user.save(update_fields=["is_staff"])


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------

@login_required
@require_http_methods(["GET", "POST"])
def manage_organizations(request):
    if not can_administer_organizations(request.user):
        raise PermissionDenied(
            "Managing organizations requires institution-administrator rights. "
            "Election-commission chairs oversee elections but do not administer "
            "the organization lifecycle."
        )
    editable = admin_organizations_for(request.user)
    scoped = editable if not is_system_admin(request.user) else Organization.objects.all()
    edit_id = request.GET.get("edit") or ""
    edit_org = editable.filter(pk=edit_id).first() if edit_id else None

    if request.method == "POST":
        action = request.POST.get("action", "create")
        if action == "create":
            if not is_system_admin(request.user):
                raise PermissionDenied("Only the Super Admin may create organizations.")
            form = OrganizationForm(request.POST)
            if form.is_valid():
                org = form.save()
                audit.record(
                    action="organization_created",
                    actor=request.user,
                    organization=org,
                    request=request,
                    object_type="organization",
                    object_id=str(org.pk),
                    details={"name": org.name, "kind": org.kind},
                )
                messages.success(request, f"Organization “{org.name}” created.")
                return redirect("manage_organizations")
        elif action == "update":
            org = get_object_or_404(editable, pk=request.POST.get("organization"))
            form = OrganizationForm(request.POST, instance=org, allowed_parents=editable)
            if form.is_valid():
                form.save()
                audit.record(
                    action="organization_updated",
                    actor=request.user,
                    organization=org,
                    request=request,
                    object_type="organization",
                    object_id=str(org.pk),
                    details={"name": org.name},
                )
                messages.success(request, f"Organization “{org.name}” updated.")
                return redirect("manage_organizations")
            messages.error(request, "Please correct the errors below.")
            edit_org = org
        elif action == "toggle":
            org = get_object_or_404(editable, pk=request.POST.get("organization"))
            org.is_active = not org.is_active
            org.save(update_fields=["is_active"])
            audit.record(
                action="organization_activated" if org.is_active else "organization_deactivated",
                actor=request.user,
                organization=org,
                request=request,
                object_type="organization",
                object_id=str(org.pk),
            )
            messages.success(
                request,
                f"Organization “{org.name}” "
                f"{'activated' if org.is_active else 'deactivated'}.",
            )
            return redirect("manage_organizations")
        else:
            messages.error(request, "Unknown action.")
            return redirect("manage_organizations")
    else:
        form = OrganizationForm(
            instance=edit_org, allowed_parents=editable
        )

    organizations = (
        scoped.annotate(
            election_count=Count("elections", distinct=True),
            member_count=Count("memberships", distinct=True),
        ).order_by("name")
    )
    return render(
        request,
        "organizations/manage_organizations.html",
        {
            "organizations": organizations,
            "form": form,
            "edit_org": edit_org,
            "can_create": is_system_admin(request.user),
            "is_admin": is_system_admin(request.user),
        },
    )


# ---------------------------------------------------------------------------
# People & access
# ---------------------------------------------------------------------------

@login_required
@require_http_methods(["GET", "POST"])
def manage_people(request):
    _manage_people_gate(request)
    editable = people_organizations_for(request.user)
    allowed_roles = _grantable_choices(request.user)

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "grant":
            form = MembershipGrantForm(
                request.POST, organizations=editable, allowed_roles=allowed_roles
            )
            if not form.is_valid():
                messages.error(request, "Could not grant role: " + form.errors.as_text())
                return redirect("manage_people")
            org = form.cleaned_data["organization"]
            user = form.cleaned_data["user"]
            role = form.cleaned_data["role"]
            if not editable.filter(pk=org.pk).exists():
                raise PermissionDenied("That organization is outside your scope.")
            if role not in grantable_roles(request.user, org):
                raise PermissionDenied(
                    "Your role does not permit granting that role here. Chairs may "
                    "appoint election officials and observers only."
                )
            with transaction.atomic():
                _membership, created = OrganizationMembership.objects.get_or_create(
                    user=user, organization=org, role=role
                )
                _sync_staff_flag(user)
                audit.record(
                    action="membership_granted",
                    actor=request.user,
                    organization=org,
                    request=request,
                    object_type="membership",
                    object_id=str(user.pk),
                    details={"user": user.get_username(), "role": role},
                )
            if created:
                messages.success(
                    request,
                    f"{user.get_username()} is now {ROLE_LABELS.get(role, role)} "
                    f"of {org.name}.",
                )
            else:
                messages.info(request, "That role assignment already exists.")
            return redirect("manage_people")

        if action == "revoke":
            membership = get_object_or_404(
                OrganizationMembership.objects.select_related("user", "organization"),
                pk=request.POST.get("membership"),
                organization__in=editable,
            )
            user, org, role = membership.user, membership.organization, membership.role
            if role not in grantable_roles(request.user, org):
                raise PermissionDenied(
                    "Your role does not permit revoking that role. Chairs may only "
                    "manage election officials and observers."
                )
            with transaction.atomic():
                membership.delete()
                _sync_staff_flag(user)
                audit.record(
                    action="membership_revoked",
                    actor=request.user,
                    organization=org,
                    request=request,
                    object_type="membership",
                    object_id=str(user.pk),
                    details={"user": user.get_username(), "role": role},
                )
            messages.success(
                request,
                f"Revoked {ROLE_LABELS.get(role, role)} from {user.get_username()} "
                f"at {org.name}.",
            )
            return redirect("manage_people")

        if action == "create_user":
            form = UserCreateForm(request.POST)
            if not form.is_valid():
                messages.error(request, "Could not create account: " + form.errors.as_text())
                return redirect("manage_people")
            with transaction.atomic():
                user = form.save()
                org_id = request.POST.get("organization") or ""
                role = request.POST.get("role") or ""
                if org_id and role in ROLE_LABELS:
                    org = editable.filter(pk=org_id).first()
                    if org is not None and role in grantable_roles(request.user, org):
                        OrganizationMembership.objects.create(
                            user=user, organization=org, role=role
                        )
                        _sync_staff_flag(user)
                        audit.record(
                            action="membership_granted",
                            actor=request.user,
                            organization=org,
                            request=request,
                            object_type="membership",
                            object_id=str(user.pk),
                            details={"user": user.get_username(), "role": role},
                        )
                audit.record(
                    action="user_created",
                    actor=request.user,
                    request=request,
                    object_type="user",
                    object_id=str(user.pk),
                    details={"username": user.get_username()},
                )
            messages.success(request, f"Account “{user.get_username()}” created.")
            return redirect("manage_people")

        messages.error(request, "Unknown action.")
        return redirect("manage_people")

    memberships = (
        OrganizationMembership.objects.filter(organization__in=editable)
        .select_related("user", "organization")
        .order_by("organization__name", "role", "user__username")
    )
    if is_system_admin(request.user) or has_system_wide_scope(request.user):
        users = User.objects.order_by("username")
    else:
        users = (
            User.objects.filter(org_memberships__organization__in=editable)
            .distinct()
            .order_by("username")
        )
    return render(
        request,
        "organizations/manage_people.html",
        {
            "memberships": memberships,
            "users": users,
            "grant_form": MembershipGrantForm(
                organizations=editable, allowed_roles=allowed_roles
            ),
            "user_form": UserCreateForm(),
            "organizations": editable.order_by("name"),
            "role_choices": [
                (value, label)
                for value, label in OrganizationMembership.ROLE_CHOICES
                if value in set(allowed_roles)
            ],
            "console_roles": CONSOLE_ROLES,
            "is_admin": is_system_admin(request.user),
            "can_admin_orgs": can_administer_organizations(request.user),
        },
    )
