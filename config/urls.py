"""URL configuration for the voting system."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("org/", include("apps.orgs.urls")),
    path("elections/", include("apps.elections.urls")),
    path("voters/", include("apps.voters.urls")),
    path("candidates/", include("apps.candidates.urls")),
    path("vote/", include("apps.ballots.urls")),
    path("tally/", include("apps.tally.urls")),
    path("audit/", include("apps.audit.urls")),
    path("monitoring/", include("apps.monitoring.urls")),
    path("", include("apps.core.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler403 = "apps.core.views.handler403"
handler404 = "apps.core.views.handler404"
handler500 = "apps.core.views.handler500"
