from django.urls import path

from . import views

app_name = "ballots"

urlpatterns = [
    path("<slug:slug>/", views.booth, name="booth"),
    path("<slug:slug>/submit/", views.submit, name="submit"),
    path("<slug:slug>/revoke/", views.revoke, name="revoke"),
]
