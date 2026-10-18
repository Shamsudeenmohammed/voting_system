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
        if obj.frozen and change:
            # No editing of a frozen election through the raw admin.
            raise PermissionDenied(
                "This election's configuration is frozen; use the audited "
                "correction procedure instead."
            )
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
        if obj.election.frozen:
            raise PermissionDenied("Configuration is frozen for this election.")
        super().save_model(request, obj, form, change)
        invalidate_config_cache(obj.election)

    def delete_model(self, request, obj):
        if obj.election.frozen:
            raise PermissionDenied("Configuration is frozen for this election.")
        election = obj.election
        super().delete_model(request, obj)
        invalidate_config_cache(election)


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ["full_name", "position", "election", "status"]
    list_filter = ["status", "position__election"]
    search_fields = ["full_name"]
    readonly_fields = ["approved_at"]

    def election(self, obj):
        return obj.position.election

    def save_model(self, request, obj, form, change):
        if obj.position.election.frozen:
            raise PermissionDenied("Configuration is frozen for this election.")
        super().save_model(request, obj, form, change)
        invalidate_config_cache(obj.position.election)

    def delete_model(self, request, obj):
        if obj.position.election.frozen:
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
