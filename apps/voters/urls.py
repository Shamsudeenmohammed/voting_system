from django.urls import path

from . import views

app_name = "voters"

urlpatterns = [
    path("login/", views.voter_login, name="voter-login"),
    path("logout/", views.voter_logout, name="voter-logout"),
    path("", views.voter_home, name="home"),
    path("registry/", views.registry_home, name="registry"),
    path("registry/list/", views.registry_list, name="registry-list"),
    path("registry/import/", views.registry_import, name="registry-import"),
    path("registry/import/<int:batch_id>/", views.import_report, name="import-report"),
    path(
        "registry/eligibility/<int:election_id>/<int:voter_id>/",
        views.eligibility_decide,
        name="eligibility-decide",
    ),
]
