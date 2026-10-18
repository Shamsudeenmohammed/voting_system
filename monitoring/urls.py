"""URL routing for live monitoring and audit export."""
from django.urls import path

from . import views

urlpatterns = [
    path("elections/<uuid:pk>/monitor/", views.monitor, name="monitor"),
    path("elections/<uuid:pk>/live/", views.live_status, name="live_status"),
    path("elections/<uuid:pk>/audit/export/", views.audit_export, name="audit_export"),
    path("org/", views.org_dashboard, name="org_dashboard"),
]
