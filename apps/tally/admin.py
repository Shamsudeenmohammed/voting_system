from django.contrib import admin

from .models import PositionResult, ReceiptRegister, ResultVersion, StationTally


class PositionResultInline(admin.TabularInline):
    model = PositionResult
    extra = 0


@admin.register(ResultVersion)
class ResultVersionAdmin(admin.ModelAdmin):
    list_display = (
        "election",
        "version_no",
        "status",
        "ballots_accepted",
        "ballots_counted",
        "reconciled",
        "tally_hash",
    )
    list_filter = ("status",)
    inlines = [PositionResultInline]
    readonly_fields = (
        "tally_hash",
        "ballots_accepted",
        "ballots_counted",
        "ballots_invalid",
        "calculated_at",
        "certified_at",
    )


admin.site.register(ReceiptRegister)
admin.site.register(StationTally)
