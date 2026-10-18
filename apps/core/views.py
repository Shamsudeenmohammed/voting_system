"""Generic views: landing, dashboard redirect, health check, error pages."""
import django
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect
from django.template import loader
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@require_GET
def landing(request):
    if request.user.is_authenticated:
        return redirect("core:dashboard-redirect")
    return redirect("accounts:login")


@login_required
@require_GET
def dashboard_redirect(request):
    """Send each role to its home surface."""
    user = request.user
    if user.is_superuser:
        return redirect("org:dashboard")
    from apps.candidates.models import Representative
    from apps.orgs.models import Membership

    if Representative.objects.filter(user=user, status="approved").exists():
        return redirect("monitoring:rep-home")
    if Membership.objects.filter(user=user, is_active=True).exists():
        return redirect("elections:home")
    return redirect("voters:home")


@require_GET
@never_cache
def health(request):
    """Health/readiness probe. No sensitive data."""
    from django.db import connection

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    status = 200 if db_ok else 503
    return JsonResponse(
        {
            "status": "ok" if db_ok else "degraded",
            "database": "up" if db_ok else "down",
            "version": django.get_version(),
            "debug": settings.DEBUG,
        },
        status=status,
    )


def handler403(request, exception=None):
    return _error_page(request, 403, "You do not have permission to view this page.")


def handler404(request, exception=None):
    return _error_page(request, 404, "The page you requested was not found.")


def handler500(request):
    return _error_page(request, 500, "Something went wrong on our side.")


def _error_page(request, code, message):
    from django.http import HttpResponse

    template = loader.get_template(f"errors/{code}.html")
    html = template.render({"message": message}, request)
    return HttpResponse(html, status=code)
