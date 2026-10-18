from django.urls import path

from . import views

app_name = "monitoring"

urlpatterns = [
    path("rep/", views.rep_home, name="rep-home"),
    path("rep/<slug:slug>/", views.rep_room, name="rep-room"),
    path("rep/<slug:slug>/incident/new/", views.rep_incident_new, name="rep-incident-new"),
    path("<slug:slug>/", views.official_dashboard, name="official-dashboard"),
    path("<slug:slug>/feed/", views.live_feed, name="live-feed"),
    path(
        "<slug:slug>/incidents/<int:pk>/update/",
        views.official_incident_update,
        name="incident-update",
    ),
]
