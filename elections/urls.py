# elections/urls.py
from django.urls import path
from . import views
from django.views.generic import TemplateView

app_name = "elections"

urlpatterns = [
    path("", views.election_list, name="list"),
    path("<uuid:election_id>/", views.election_detail, name="detail"),
    path("<uuid:election_id>/start/", views.start_vote, name="start_vote"),
    path("<uuid:election_id>/ballot/", views.ballot_view, name="ballot"),
    path("vote/confirmation/<uuid:vote_id>/", views.vote_confirmation, name="vote_confirm"),
    path("<uuid:election_id>/login/", views.voter_login, name="voter_login"),
]
