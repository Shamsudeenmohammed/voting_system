"""URL routing for representative accreditation and monitoring surfaces."""
from django.urls import path

from . import views

urlpatterns = [
    path("elections/<uuid:pk>/representatives/", views.apply, name="apply"),
    path(
        "elections/<uuid:pk>/representatives/list/",
        views.representatives_list,
        name="representatives_list",
    ),
    path("representatives/<uuid:rep_pk>/decide/", views.decide, name="decide"),
    path("representatives/<uuid:rep_pk>/accredit/", views.accredit, name="accredit"),
    path("representatives/", views.dashboard, name="representative_dashboard"),
    path(
        "elections/<uuid:pk>/incidents/new/",
        views.incident_create,
        name="incident_create",
    ),
    path("elections/<uuid:pk>/incidents/", views.incident_list, name="incident_list"),
    path(
        "incidents/<uuid:incident_pk>/resolve/",
        views.incident_resolve,
        name="incident_resolve",
    ),
]
