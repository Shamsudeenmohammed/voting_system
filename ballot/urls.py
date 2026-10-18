"""URL routing for the ballot app (voter-facing only)."""
from django.urls import path

from . import views

urlpatterns = [
    path("ballot/<str:token>/", views.prepare, name="ballot_prepare"),
    path("elections/<uuid:pk>/receipts/", views.receipt_lookup, name="receipt_lookup"),
    path("robots.txt", views.robots, name="robots"),
]
