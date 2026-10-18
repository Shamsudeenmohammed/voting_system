"""Organization administration views."""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.core.permissions import require_org_admin, user_orgs
from .models import Membership, Organization, Unit

User = get_user_model()


@login_required
@never_cache
def dashboard(request):
    memberships = list(user_orgs(request.user).select_related("organization"))
    orgs = [m.organization for m in memberships]
    if request.user.is_superuser and not orgs:
        orgs = list(Organization.objects.filter(is_active=True))
    stats = {
        "elections": 0,
        "voters": 0,
    }
    from apps.elections.models import Election
    from apps.voters.models import Voter

    if orgs:
        stats["elections"] = Election.objects.filter(organization__in=orgs).count()
        stats["voters"] = Voter.objects.filter(organization__in=orgs).count()
    return render(
        request,
        "orgs/dashboard.html",
        {"orgs": orgs, "memberships": memberships, "stats": stats},
    )


@login_required
@require_http_methods(["GET", "POST"])
def org_create(request):
    if not request.user.is_superuser:
        # Any authenticated user may *request* nothing — creation is admin-only.
        raise PermissionDenied("Only platform administrators create organizations.")
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        slug = request.POST.get("slug", "").strip()
        org_type = request.POST.get("org_type", "other")
        if name and slug:
            org, created = Organization.objects.get_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "org_type": org_type if org_type in dict(Organization.OrgType.choices) else "other",
                    "contact_email": request.POST.get("contact_email", ""),
                },
            )
            if created:
                Membership.objects.create(
                    user=request.user, organization=org, role=Membership.Role.ADMIN
                )
                messages.success(request, f"Organization '{org.name}' created.")
                return redirect("org:dashboard")
            messages.error(request, "Slug already in use.")
    return render(request, "orgs/form.html")


@login_required
@never_cache
def org_detail(request, slug):
    org = get_object_or_404(Organization, slug=slug)
    require_org_admin(request.user, org)
    memberships = org.memberships.select_related("user").order_by("role", "user__username")
    units = org.units.order_by("unit_type", "name")
    return render(
        request,
        "orgs/detail.html",
        {"org": org, "memberships": memberships, "units": units},
    )


@login_required
@require_http_methods(["POST"])
def member_add(request, slug):
    org = get_object_or_404(Organization, slug=slug)
    require_org_admin(request.user, org)
    username = request.POST.get("username", "").strip()
    role = request.POST.get("role", Membership.Role.OBSERVER)
    user = User.objects.filter(username=username).first()
    if user is None:
        messages.error(request, f"No user named '{username}'.")
    elif role not in dict(Membership.Role.choices):
        messages.error(request, "Invalid role.")
    else:
        Membership.objects.update_or_create(
            user=user,
            organization=org,
            defaults={"role": role, "is_active": True},
        )
        messages.success(request, f"{user} → {dict(Membership.Role.choices)[role]}.")
    return redirect("org:detail", slug=slug)


@login_required
@require_http_methods(["POST"])
def member_toggle(request, slug, pk):
    org = get_object_or_404(Organization, slug=slug)
    require_org_admin(request.user, org)
    membership = get_object_or_404(Membership, pk=pk, organization=org)
    if membership.user_id == request.user.id:
        messages.error(request, "You cannot deactivate your own membership.")
    else:
        membership.is_active = not membership.is_active
        membership.save(update_fields=["is_active", "updated_at"])
        messages.success(
            request,
            f"Membership {'activated' if membership.is_active else 'deactivated'}.",
        )
    return redirect("org:detail", slug=slug)


@login_required
@require_http_methods(["POST"])
def unit_create(request, slug):
    org = get_object_or_404(Organization, slug=slug)
    require_org_admin(request.user, org)
    name = request.POST.get("name", "").strip()
    unit_type = request.POST.get("unit_type", "other")
    parent_id = request.POST.get("parent") or None
    if name and unit_type in dict(Unit.UnitType.choices):
        unit, created = Unit.objects.get_or_create(
            organization=org,
            name=name,
            unit_type=unit_type,
            defaults={"parent_id": parent_id},
        )
        messages.success(
            request, "Unit added." if created else "Unit already exists."
        )
    return redirect("org:detail", slug=slug)
