from django.urls import path

from . import views

app_name = "elections"

urlpatterns = [
    path("", views.home, name="home"),
    path("new/", views.create, name="create"),
    path("<slug:slug>/", views.detail, name="detail"),
    path("<slug:slug>/edit/", views.edit, name="edit"),
    path("<slug:slug>/transition/", views.transition, name="transition"),
    path("<slug:slug>/reopen/", views.reopen, name="reopen"),
    path("<slug:slug>/positions/new/", views.position_create, name="position-create"),
    path(
        "<slug:slug>/stations/<int:station_id>/status/",
        views.station_set_status,
        name="station-status",
    ),
    path("<slug:slug>/milestones/new/", views.milestone_create, name="milestone-create"),
]
