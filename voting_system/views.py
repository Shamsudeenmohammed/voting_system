"""Project-level error handlers.

The product surfaces explain *why* an action was refused (which permission or
approval is missing and what the next workflow is) by raising
``PermissionDenied`` with a specific message.  These handlers turn that message
into a friendly, navigable page in production (when ``DEBUG`` is off).  With
``DEBUG`` on, Django's technical 403 page is shown instead, which still includes
the same message.

They render no sensitive data: the message comes from the raised
``PermissionDenied`` only, never from request parameters.
"""
from __future__ import annotations

from django.shortcuts import render

DEFAULT_403 = (
    "You do not have the permission required for this action. Ask an "
    "institution administrator or the election commission chair to grant you "
    "the right role, or use the workflow referenced on the page."
)


def permission_denied(request, exception=None):
    message = DEFAULT_403
    if exception is not None:
        text = str(exception).strip()
        if text:
            message = text
    return render(request, "403.html", {"message": message}, status=403)

