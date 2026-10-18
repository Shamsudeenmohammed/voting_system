from django.contrib import admin

from .models import Election, ElectionMilestone, LifecycleEvent, PollingStation, Position


@admin.register(Election)
class ElectionAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "state", "scheduled_opens", "scheduled_closes")
    list_filter = ("state", "organization")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = (
        "state",
        "opened_at",
        "closed_at",
        "closed_by",
        "config_frozen_at",
        "config_frozen_hash",
        "reopen_count",
        "updated_at",
    )


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ("name", "election", "max_choices", "is_active")
    search_fields = ("name", "slug")


@admin.register(PollingStation)
class PollingStationAdmin(admin.ModelAdmin):
    list_display = ("name", "election", "channel", "status")
    list_filter = ("channel", "status")


admin.site.register(ElectionMilestone)
admin.site.register(LifecycleEvent)
