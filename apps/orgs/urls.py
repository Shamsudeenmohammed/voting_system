from django.urls import path

from . import views

app_name = "org"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("new/", views.org_create, name="create"),
    path("<slug:slug>/", views.org_detail, name="detail"),
    path("<slug:slug>/members/add/", views.member_add, name="member-add"),
    path("<slug:slug>/members/<int:pk>/toggle/", views.member_toggle, name="member-toggle"),
    path("<slug:slug>/units/add/", views.unit_create, name="unit-create"),
]
