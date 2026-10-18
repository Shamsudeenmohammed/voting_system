from django.urls import path

from . import views

app_name = "audit"

urlpatterns = [
    path("", views.event_list, name="events"),
    path("verify/", views.verify_page, name="verify"),
    path("verify/run/", views.run_verify, name="run-verify"),
    path("checkpoint/", views.checkpoint, name="checkpoint"),
    path("checkpoints/", views.checkpoint_list, name="checkpoints"),
]
