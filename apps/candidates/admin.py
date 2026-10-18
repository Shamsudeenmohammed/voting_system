from django.contrib import admin

from .models import Candidate, Incident, Representative


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ("full_name", "election", "position", "status")
    list_filter = ("status", "election")
    search_fields = ("full_name", "slug")


@admin.register(Representative)
class RepresentativeAdmin(admin.ModelAdmin):
    list_display = ("user", "candidate", "election", "status", "attendance")
    list_filter = ("status", "election")


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ("title", "election", "category", "status", "filed_by")
    list_filter = ("status", "category", "election")
