from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("watch/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("monitoring/", include("monitoring.urls")),
    path("", include("elections.urls")),
    path("", include("ballot.urls")),
    path("", include("tally.urls")),
    path("", include("representatives.urls")),
    path("", include("monitoring.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
