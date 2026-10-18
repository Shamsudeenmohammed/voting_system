"""URL routing for the elections app (public + management surfaces)."""
from django.urls import path

from . import views

urlpatterns = [
    # public
    path("", views.home, name="home"),
    path("elections/<uuid:pk>/", views.election_detail, name="election_detail"),
    path("elections/<uuid:pk>/access/", views.voter_access, name="voter_access"),
    # management
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/elections/new/", views.election_create, name="election_create"),
    path("dashboard/elections/<uuid:pk>/edit/", views.election_edit, name="election_edit"),
    path("dashboard/elections/<uuid:pk>/config/", views.election_config, name="election_config"),
    path("dashboard/elections/<uuid:pk>/voters/", views.election_voters, name="election_voters"),
    path("dashboard/elections/<uuid:pk>/audit/", views.election_audit, name="election_audit"),
    path(
        "dashboard/elections/<uuid:pk>/audit/checkpoint/",
        views.audit_checkpoint,
        name="audit_checkpoint",
    ),
    path("dashboard/elections/<uuid:pk>/action/", views.election_action, name="election_action"),
    path(
        "dashboard/elections/<uuid:pk>/approvals/new/",
        views.approval_request_view,
        name="approval_request",
    ),
    path(
        "dashboard/approvals/<uuid:approval_pk>/decide/",
        views.approval_decide_view,
        name="approval_decide",
    ),
    # ballot structure
    path(
        "dashboard/elections/<uuid:pk>/positions/new/",
        views.position_add,
        name="position_add",
    ),
    path(
        "dashboard/elections/<uuid:pk>/positions/<uuid:position_id>/delete/",
        views.position_delete,
        name="position_delete",
    ),
    path(
        "dashboard/elections/<uuid:pk>/positions/<uuid:position_id>/candidates/new/",
        views.candidate_add,
        name="candidate_add",
    ),
    path(
        "dashboard/elections/<uuid:pk>/candidates/<uuid:candidate_id>/delete/",
        views.candidate_delete,
        name="candidate_delete",
    ),
    path(
        "dashboard/elections/<uuid:pk>/stations/new/",
        views.station_add,
        name="station_add",
    ),
    # voter registry
    path(
        "dashboard/elections/<uuid:pk>/voters/add/",
        views.voter_add,
        name="voter_add",
    ),
    path(
        "dashboard/elections/<uuid:pk>/voters/bulk/",
        views.voter_bulk_add,
        name="voter_bulk_add",
    ),
    path(
        "dashboard/elections/<uuid:pk>/voters/<uuid:voter_pk>/revoke/",
        views.voter_revoke,
        name="voter_revoke",
    ),
]
