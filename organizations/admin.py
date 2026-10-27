"""Django admin registration for tenancy models.

The product console (``organizations.views``) is the primary surface; the
admin is registered as a safety hatch for operators and mirrors the same
scoping where the admin allows it.
"""
from django.contrib import admin

from .models import Organization, OrganizationMembership


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ["name", "kind", "parent", "is_active", "member_count", "election_count"]
    list_filter = ["kind", "is_active", "parent"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ["parent"]

    def member_count(self, obj):
        return obj.memberships.count()

    def election_count(self, obj):
        return obj.elections.count()

    member_count.short_description = "Members"
    election_count.short_description = "Elections"


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "organization", "role", "created_at"]
    list_filter = ["role", "organization"]
    search_fields = ["user__username", "user__email", "organization__name"]
    autocomplete_fields = ["user", "organization"]
