from django.urls import path
from . import views

app_name = "elections"

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.voter_login_view, name="voter_login"),
    path("login/admin/", views.admin_login_view, name="admin_login"),
    path("logout/", views.logout_view, name="logout"),
    path("elections/", views.election_list, name="list"),
    path("elections/<uuid:election_id>/", views.election_detail, name="detail"),
    path("elections/<uuid:election_id>/ballot/", views.ballot, name="ballot"),
    path("elections/<uuid:election_id>/confirmed/", views.vote_confirmation, name="vote_confirmation"),
    path("elections/<uuid:election_id>/results/", views.results, name="results"),
    path("api/elections/<uuid:election_id>/results/", views.api_results, name="api_results"),

    # Custom Admin (using /dashboard/ to avoid Django admin conflict)
    path("dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("dashboard/elections/new/", views.admin_election_new, name="admin_election_new"),
    path("dashboard/elections/<uuid:election_id>/", views.admin_election_detail, name="admin_election_detail"),
    path("dashboard/elections/<uuid:election_id>/edit/", views.admin_election_edit, name="admin_election_edit"),
    path("dashboard/elections/<uuid:election_id>/delete/", views.admin_election_delete, name="admin_election_delete"),
    path("dashboard/elections/<uuid:election_id>/toggle/", views.admin_election_toggle, name="admin_election_toggle"),

    path("dashboard/elections/<uuid:election_id>/positions/new/", views.admin_position_new, name="admin_position_new"),
    path("dashboard/elections/<uuid:election_id>/positions/<uuid:position_id>/edit/", views.admin_position_edit, name="admin_position_edit"),
    path("dashboard/elections/<uuid:election_id>/positions/<uuid:position_id>/delete/", views.admin_position_delete, name="admin_position_delete"),

    path("dashboard/elections/<uuid:election_id>/positions/<uuid:position_id>/candidates/new/", views.admin_candidate_new, name="admin_candidate_new"),
    path("dashboard/elections/<uuid:election_id>/positions/<uuid:position_id>/candidates/<uuid:candidate_id>/edit/", views.admin_candidate_edit, name="admin_candidate_edit"),
    path("dashboard/elections/<uuid:election_id>/positions/<uuid:position_id>/candidates/<uuid:candidate_id>/delete/", views.admin_candidate_delete, name="admin_candidate_delete"),

    path("dashboard/elections/<uuid:election_id>/voters/", views.admin_voters, name="admin_voters"),
    path("api/elections/<uuid:election_id>/voters/", views.api_voters, name="api_voters"),
    path("dashboard/elections/<uuid:election_id>/voters/upload/", views.admin_voters_upload, name="admin_voters_upload"),
    path("dashboard/elections/<uuid:election_id>/voters/<uuid:voter_id>/reset/", views.admin_voter_reset, name="admin_voter_reset"),
    path("dashboard/elections/<uuid:election_id>/voters/<uuid:voter_id>/regenerate-token/", views.admin_voter_regenerate_token, name="admin_voter_regenerate_token"),
]
