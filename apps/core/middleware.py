"""Shared middleware."""
from django.conf import settings


class ActiveOrganizationMiddleware:
    """Attach the user's active organization to the request.

    Multi-tenancy rule: views/services must filter querysets by
    ``request.active_org`` (or the object's organization) — never trust the
    client to name the organization. Superusers see everything only through
    explicit admin views.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.active_org = None
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            from apps.orgs.models import Membership

            org = (
                Membership.objects.filter(user=user, is_active=True)
                .select_related("organization")
                .order_by("-role")
                .first()
            )
            if org:
                request.active_org = org.organization
        return self.get_response(request)
