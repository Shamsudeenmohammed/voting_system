from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils import timezone
from .models import Election, Position, Candidate, Voter, Vote, AuditLog


class CandidateInline(admin.TabularInline):
    model = Candidate
    extra = 1
    fields = ["full_name", "order", "photo"]


class PositionInline(admin.StackedInline):
    model = Position
    extra = 1
    show_change_link = True


@admin.register(Election)
class ElectionAdmin(admin.ModelAdmin):
    list_display = ["title", "category", "ballot_type", "start_at", "end_at", "status", "turnout"]
    list_filter = ["category", "ballot_type", "is_published"]
    search_fields = ["title"]
    inlines = [PositionInline]
    actions = ["duplicate"]

    fieldsets = [
        (None, {"fields": ["title", "description", "category"]}),
        ("Configuration", {"fields": ["ballot_type", "start_at", "end_at"]}),
        ("Visibility", {"fields": ["is_published", "show_results"]}),
    ]

    def status(self, obj):
        if obj.is_open():
            return format_html('<span style="color:#059669;font-weight:600;">Open</span>')
        if obj.is_published and obj.end_at < timezone.now():
            return format_html('<span style="color:#6b7280;">Closed</span>')
        return format_html('<span style="color:#d97706;">Scheduled</span>')

    def turnout(self, obj):
        t = obj.total_voters()
        v = obj.voted_count()
        pct = (v / t * 100) if t else 0
        return f"{v}/{t} ({pct:.0f}%)"

    def duplicate(self, request, queryset):
        for e in queryset:
            e.pk = None
            e.title = f"{e.title} (copy)"
            e.is_published = False
            e.save()
        self.message_user(request, f"{queryset.count()} duplicated.")
    duplicate.short_description = "Duplicate selected elections"


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ["title", "election", "candidate_count"]
    list_filter = ["election"]
    search_fields = ["title"]
    inlines = [CandidateInline]

    def candidate_count(self, obj):
        return obj.candidates.count()
    candidate_count.short_description = "Candidates"


@admin.register(Voter)
class VoterAdmin(admin.ModelAdmin):
    list_display = ["voter_id", "full_name", "email", "election", "token_short", "has_voted", "voted_at"]
    list_filter = ["election", "has_voted"]
    search_fields = ["voter_id", "full_name", "email"]
    actions = ["reset_vote"]

    def token_short(self, obj):
        return obj.token[:8] + "..." if len(obj.token) > 8 else obj.token
    token_short.short_description = "Token"

    def reset_vote(self, request, queryset):
        updated = queryset.update(has_voted=False, voted_at=None)
        Vote.objects.filter(voter__in=queryset).delete()
        self.message_user(request, f"{updated} voter(s) reset.")
    reset_vote.short_description = "Reset vote for selected voters"


@admin.register(Vote)
class VoteAdmin(admin.ModelAdmin):
    list_display = ["id", "election", "voter", "created_at"]
    list_filter = ["election"]
    readonly_fields = ["election", "voter", "selections", "created_at", "ip_address"]


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["timestamp", "action", "user"]
    list_filter = ["action"]
    readonly_fields = ["timestamp"]
