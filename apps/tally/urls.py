from django.urls import path

from . import views

app_name = "tally"

urlpatterns = [
    path("<slug:slug>/", views.results, name="results"),
    path("<slug:slug>/run/", views.run_tally, name="run"),
    path("<slug:slug>/certify/", views.certify, name="certify"),
    path("<slug:slug>/verify/", views.verify, name="verify"),
    path("<slug:slug>/receipts/", views.receipt_register, name="receipts"),
    path("public/<slug:slug>/", views.public_results, name="public"),
]
