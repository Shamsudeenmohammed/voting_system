"""URL routing for the elections app (public + management surfaces)."""
from django.urls import path

from . import views

urlpatterns = [
    # public
    path("", views.home, name="home"),
    path("elections/<uuid:pk>/", views.election_detail, name="election_detail"),
    path("elections/<uuid:pk>/access/", views.voter_access, name="voter_access"),
    path("elections/<uuid:pk>/login/", views.voter_login, name="voter_login"),
    # management
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/chair/", views.chair_dashboard, name="chair_dashboard"),
    # cross-election console
    path("dashboard/operations/", views.operations, name="operations"),
    path("dashboard/approvals/", views.approvals_inbox, name="approvals_inbox"),
    path("dashboard/results/", views.results_index, name="results_index"),
    path("dashboard/audit/", views.audit_overview, name="audit_overview"),
    path("dashboard/settings/", views.settings_overview, name="settings_overview"),
    path("dashboard/elections/new/", views.election_create, name="election_create"),
    path("dashboard/elections/<uuid:pk>/edit/", views.election_edit, name="election_edit"),
    path("dashboard/elections/<uuid:pk>/config/", views.election_config, name="election_config"),
    path("dashboard/elections/<uuid:pk>/voters/", views.election_voters, name="election_voters"),
    path("dashboard/elections/<uuid:pk>/polling-officer/", views.polling_officer, name="polling_officer"),
    path("dashboard/elections/<uuid:pk>/audit/", views.election_audit, name="election_audit"),
    path(
        "dashboard/elections/<uuid:pk>/audit/checkpoint/",
        views.audit_checkpoint,
        name="audit_checkpoint",
    ),
    path("dashboard/elections/<uuid:pk>/action/", views.election_action, name="election_action"),
    path(
        "dashboard/elections/<uuid:pk>/governance/",
        views.election_governance,
        name="election_governance",
    ),
    path(
        "dashboard/elections/<uuid:pk>/override/",
        views.election_override,
        name="election_override",
    ),
    path(
        "dashboard/elections/<uuid:pk>/delete/",
        views.election_delete,
        name="election_delete",
    ),
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
        "dashboard/elections/<uuid:pk>/candidates/<uuid:candidate_id>/edit/",
        views.candidate_edit,
        name="candidate_edit",
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
        "dashboard/voter-registry/",
        views.voter_registry_index,
        name="voter_registry_index",
    ),
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
        "dashboard/elections/<uuid:pk>/voters/upload/",
        views.voter_file_add,
        name="voter_file_add",
    ),
    path(
        "dashboard/elections/<uuid:pk>/voters/template/",
        views.voter_registry_template,
        name="voter_registry_template",
    ),
    path(
        "dashboard/elections/<uuid:pk>/voters/<uuid:voter_pk>/revoke/",
        views.voter_revoke,
        name="voter_revoke",
    ),
    # Workstation management & access modes
    path("workstation/login/", views.workstation_login, name="workstation-login"),
    path("workstation/login/", views.workstation_login, name="workstation_login"),
    path("workstation/logout/", views.workstation_logout, name="workstation-logout"),
    path("workstation/logout/", views.workstation_logout, name="workstation_logout"),
    path("workstation/voting/", views.workstation_voting, name="workstation-voting"),
    path("workstation/voting/", views.workstation_voting, name="workstation_voting"),
    path("workstation/verification/", views.workstation_verification, name="workstation-verification"),
    path("workstation/verification/", views.workstation_verification, name="workstation_verification"),
    path("workstation/manage/", views.workstation_manage, name="workstation-manage"),
    path("workstation/manage/", views.workstation_manage, name="workstation_manage"),
    path("kiosk/", views.workstation_voting, name="kiosk"),
]
