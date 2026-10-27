"""Django admin registrations.

Notes:
* Election lifecycle fields (``state`` and timestamps) are read-only here -
  states only change through ``elections.lifecycle`` (audited, permissioned).
* Configuration edits through admin are blocked once the configuration is
  frozen, and they invalidate the cached configuration used for ballot
  validation.
* ``Vote`` / ``AuditLog`` / ``Voter.token`` no longer exist (secret-ballot
  architecture, hash-chained audit log).
"""

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from django.utils.html import format_html

from .config import invalidate_config_cache
from .models import (
    Candidate,
    Election,
    ElectionApproval,
    PollingStation,
    Position,
    Voter,
)


class CandidateInline(admin.TabularInline):
    model = Candidate
    extra = 1
    fields = ["full_name", "status", "order", "photo"]


class PositionInline(admin.StackedInline):
    model = Position
    extra = 1
    show_change_link = True
    fields = ["title", "description", "order", "max_choices"]


@admin.register(Election)
class ElectionAdmin(admin.ModelAdmin):
    list_display = [
        "title", "organization", "state_badge", "start_at", "end_at", "turnout",
    ]
    list_filter = ["category", "state", "results_visibility", "organization"]
    search_fields = ["title", "organization__name"]
    inlines = [PositionInline]
    readonly_fields = [
        "state", "state_updated_at", "opened_at", "closed_at", "certified_at",
        "published_at", "registry_frozen_at", "config_frozen_at", "config_hash",
        "reopened_count", "created_at", "created_by",
    ]

    def get_readonly_fields(self, request, obj=None):
        # Super admins may edit the lifecycle state directly (escape hatch).
        fields = list(self.readonly_fields)
        if request.user.is_superuser and "state" in fields:
            fields.remove("state")
        return fields

    fieldsets = [
        (None, {"fields": ["organization", "title", "description", "category"]}),
        ("Configuration", {"fields": ["ballot_type", "start_at", "end_at", "allow_blank"]}),
        ("Rules & visibility", {
            "fields": ["results_visibility", "eligibility_units", "rules", "is_published", "show_results"],
        }),
        ("Lifecycle (managed by the state machine)", {
            "fields": [
                "state", "state_updated_at", "opened_at", "closed_at", "certified_at",
                "published_at", "registry_frozen_at", "config_frozen_at", "config_hash",
                "reopened_count",
            ],
        }),
        ("Metadata", {"fields": ["created_by", "created_at"]}),
    ]

    def state_badge(self, obj):
        return format_html(
            '<span class="badge {}">{}</span>', obj.state_badge, obj.get_state_display()
        )
    state_badge.short_description = "State"

    def turnout(self, obj):
        t = obj.total_voters()
        v = obj.voted_count()
        pct = (v / t * 100) if t else 0
        return f"{v}/{t} ({pct:.0f}%)"

    def save_model(self, request, obj, form, change):
        # Frozen elections are immutable through the raw admin for ordinary
        # staff, but super admins are allowed to update them directly.
        if obj.frozen and change and not request.user.is_superuser:
            raise PermissionDenied(
                "This election's configuration is frozen; use the audited "
                "correction procedure instead."
            )
        if change and request.user.is_superuser:
            try:
                previous = Election.objects.get(pk=obj.pk)
            except Election.DoesNotExist:
                previous = None
            if previous is not None and previous.state != obj.state:
                obj.state_updated_at = timezone.now()
        invalidate_config_cache(obj)
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        result = super().save_formset(request, form, formset, change)
        if hasattr(form, "instance"):
            invalidate_config_cache(form.instance)
        return result


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ["title", "election", "max_choices", "candidate_count"]
    list_filter = ["election"]
    search_fields = ["title"]
    inlines = [CandidateInline]

    def candidate_count(self, obj):
        return obj.candidates.count()
    candidate_count.short_description = "Candidates"

    def save_model(self, request, obj, form, change):
        if obj.election.frozen and not request.user.is_superuser:
            raise PermissionDenied("Configuration is frozen for this election.")
        super().save_model(request, obj, form, change)
        invalidate_config_cache(obj.election)

    def delete_model(self, request, obj):
        if obj.election.frozen and not request.user.is_superuser:
            raise PermissionDenied("Configuration is frozen for this election.")
        election = obj.election
        super().delete_model(request, obj)
        invalidate_config_cache(election)


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ["photo_thumbnail", "full_name", "position", "election", "status"]
    list_filter = ["status", "position__election"]
    search_fields = ["full_name"]
    readonly_fields = ["approved_at", "photo_preview"]
    fields = ["photo_preview", "photo", "position", "full_name", "bio", "manifesto", "order", "status", "approved_at"]

    @admin.display(description="Election")
    def election(self, obj):
        return obj.position.election

    @admin.display(description="Photo")
    def photo_thumbnail(self, obj):
        """Small passport-photo thumbnail in the candidate list."""
        if obj.photo:
            return format_html(
                '<img src="{}" alt="Photograph of {}" '
                'style="width:44px;height:56px;object-fit:cover;border-radius:4px;'
                'border:1px solid var(--border, #e2e8f0);" />',
                obj.photo.url,
                obj.full_name,
            )
        return format_html(
            '<span title="No passport photograph" '
            'style="display:inline-flex;width:44px;height:56px;align-items:center;'
            'justify-content:center;border-radius:4px;background:#eef2ff;'
            'color:#4338ca;font-weight:600;font-size:13px;">{}</span>',
            obj.initials,
        )

    @admin.display(description="Passport photograph")
    def photo_preview(self, obj):
        """Large preview on the candidate detail/change page."""
        if obj.pk and obj.photo:
            return format_html(
                '<img src="{}" alt="Photograph of {}" '
                'style="max-width:240px;max-height:300px;object-fit:cover;'
                'border-radius:8px;border:1px solid var(--border, #e2e8f0);" />',
                obj.photo.url,
                obj.full_name,
            )
        return "No passport photograph uploaded yet."

    def save_model(self, request, obj, form, change):
        if obj.position.election.frozen and not request.user.is_superuser:
            raise PermissionDenied("Configuration is frozen for this election.")
        super().save_model(request, obj, form, change)
        invalidate_config_cache(obj.position.election)

    def delete_model(self, request, obj):
        if obj.position.election.frozen and not request.user.is_superuser:
            raise PermissionDenied("Configuration is frozen for this election.")
        election = obj.position.election
        super().delete_model(request, obj)
        invalidate_config_cache(election)


@admin.register(Voter)
class VoterAdmin(admin.ModelAdmin):
    list_display = [
        "voter_id", "full_name", "election", "status", "has_voted", "voted_at",
        "credentials_issued_at",
    ]
    list_filter = ["status", "has_voted", "election"]
    search_fields = ["voter_id", "full_name", "email"]
    readonly_fields = ["has_voted", "voted_at", "credentials_issued_at", "created_at"]

    def has_delete_permission(self, request, obj=None):
        # Voters who already cast a ballot must never be deleted (turnout and
        # reconciliation evidence).
        if obj is not None and obj.has_voted:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(PollingStation)
class PollingStationAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "election", "channel", "status"]
    list_filter = ["election", "status", "channel"]
    search_fields = ["name", "code", "location"]


@admin.register(ElectionApproval)
class ElectionApprovalAdmin(admin.ModelAdmin):
    list_display = ["election", "kind", "status", "requested_by", "requested_at", "decided_by"]
    list_filter = ["kind", "status"]
    readonly_fields = [f.name for f in ElectionApproval._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

import elections.admin_workstation  # noqa: F401

