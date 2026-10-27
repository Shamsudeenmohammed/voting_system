import os

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve as media_serve

urlpatterns = [
    path("watch/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    # Tenancy / people console (staff-only views).
    path("manage/", include("organizations.urls")),
    # Everything else is mounted at the root; each app owns a disjoint set of
    # prefixes (elections/, dashboard/, ballot/, monitoring via elections/…).
    path("", include("elections.urls")),
    path("", include("ballot.urls")),
    path("", include("tally.urls")),
    path("", include("representatives.urls")),
    path("", include("monitoring.urls")),
]

if settings.SERVE_MEDIA:
    # Serve media files (candidate passport photographs) directly from Django.
    #
    # static() returns [] unless DEBUG is True, so an explicit re_path is used
    # to keep media working in containerised deployments that do not yet have
    # a fronting web server (e.g. the docker-compose stack). Production
    # deployments should set SERVE_MEDIA=False and serve MEDIA_ROOT with nginx
    # (or similar) instead; see docs/DEPLOY_ONLINE.md.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += [
        re_path(
            r"^media/(?P<path>.*)$",
            media_serve,
            {"document_root": os.path.normpath(settings.MEDIA_ROOT)},
        ),
    ]

# Blocked actions explain which permission/approval is required and what the
# next workflow is (the message comes from the raised ``PermissionDenied``).
handler403 = "voting_system.views.permission_denied"
