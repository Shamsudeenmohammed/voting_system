from django.contrib import admin

from .models import EligibilityDecision, EligibilityRule, Voter, VoterCredential, VoterImportBatch


@admin.register(Voter)
class VoterAdmin(admin.ModelAdmin):
    list_display = ("voter_number", "full_name", "organization", "status", "is_verified")
    list_filter = ("organization", "status", "is_verified")
    search_fields = ("voter_number", "full_name", "email")
    # Never expose credentials in admin list displays.
    readonly_fields = ("created_at", "updated_at")


@admin.register(VoterCredential)
class VoterCredentialAdmin(admin.ModelAdmin):
    list_display = ("voter", "failed_attempts", "locked_until", "last_login_at")
    search_fields = ("voter__voter_number",)


admin.site.register(VoterImportBatch)
admin.site.register(EligibilityRule)
admin.site.register(EligibilityDecision)
