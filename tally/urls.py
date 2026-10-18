"""URL routing for tally results, recounts and exports."""
from django.urls import path

from . import views

urlpatterns = [
    path("elections/<uuid:pk>/results/", views.results, name="results"),
    path("elections/<uuid:pk>/recount/", views.recount, name="recount"),
    path(
        "elections/<uuid:pk>/results/v<int:version>/",
        views.snapshot_detail,
        name="snapshot_detail",
    ),
    path(
        "elections/<uuid:pk>/results/v<int:version>/verify/",
        views.verify,
        name="verify",
    ),
    path(
        "elections/<uuid:pk>/results/v<int:version>/export/",
        views.export_snapshot,
        name="export_snapshot",
    ),
]
