from django.contrib import admin

from .models import AcceptedBallot, BallotGrant


@admin.register(BallotGrant)
class BallotGrantAdmin(admin.ModelAdmin):
    """Identity side — selectable, but never shows ballots (no link exists)."""

    list_display = ("election", "voter_ref", "status", "issued_at", "expires_at")
    list_filter = ("status", "election")
    search_fields = ("voter_ref",)
    readonly_fields = ("token_hash", "voter_ref", "election", "issued_at")


@admin.register(AcceptedBallot)
class AcceptedBallotAdmin(admin.ModelAdmin):
    """Selection side — sealed only. No decryption UI exists by design;
    decryption happens exclusively in the tally engine."""

    list_display = ("short_id", "election", "station", "accepted_at", "payload_sha256")
    list_filter = ("election",)
    search_fields = ("receipt_hash", "submission_id")
    readonly_fields = [f.name for f in AcceptedBallot._meta.fields]

    @admin.display(description="Ballot")
    def short_id(self, obj):
        return str(obj.pk)[:8]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
