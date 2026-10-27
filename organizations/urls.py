"""URL routing for the organization / people-access console."""
from django.urls import path

from . import control_center, views

urlpatterns = [
    path("organizations/", views.manage_organizations, name="manage_organizations"),
    path("people/", views.manage_people, name="manage_people"),
    # Super Admin control centre (superuser-only; each view re-checks the gate).
    path("control/", control_center.control_center, name="control_center"),
    path(
        "control/search/",
        control_center.control_center_search,
        name="control_center_search",
    ),
    path(
        "control/overrides/",
        control_center.control_center_overrides,
        name="control_center_overrides",
    ),
    path(
        "control/recovery/",
        control_center.control_center_recovery,
        name="control_center_recovery",
    ),
]
