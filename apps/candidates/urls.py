from django.urls import path

from . import views

app_name = "candidates"

urlpatterns = [
    path("<slug:slug>/", views.candidate_list, name="list"),
    path("<slug:slug>/new/", views.candidate_create, name="create"),
    path("<slug:slug>/<int:pk>/review/", views.candidate_review, name="review"),
    path("<slug:slug>/representatives/register/", views.rep_register, name="rep-register"),
    path("<slug:slug>/representatives/", views.rep_approvals, name="rep-approvals"),
    path("<slug:slug>/representatives/<int:pk>/review/", views.rep_review, name="rep-review"),
    path("<slug:slug>/incidents/", views.incident_list, name="incidents"),
]
