"""Template context processors."""
from __future__ import annotations

from .navigation import build_navigation
from .roles import access_flags


def navigation(request):
    """Expose the role-aware sidebar model and display-only access flags."""
    return {
        "nav_groups": build_navigation(request),
        "access": access_flags(request.user),
    }
