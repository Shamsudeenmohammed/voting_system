"""Template context shared by every page."""
from django.conf import settings


def site_context(request):
    org = getattr(request, "active_org", None)
    user = getattr(request, "user", None)
    is_rep = False
    if user is not None and user.is_authenticated:
        from apps.candidates.models import Representative

        is_rep = Representative.objects.filter(
            user=user, status=Representative.Status.APPROVED
        ).exists()
    return {
        "SITE_NAME": "SecureVote",
        "active_org": org,
        "user_is_representative": is_rep,
        "rate_limit_hints": {},  # reserved for UI hints; never contains voter data
    }
