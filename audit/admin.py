from django.contrib import admin

from .models import AuditChain, AuditCheckpoint, AuditEvent


@admin.register(AuditChain)
class AuditChainAdmin(admin.ModelAdmin):
    list_display = ["chain_key", "last_seq", "head_hash_short", "updated_at"]
    search_fields = ["chain_key"]
    readonly_fields = ["chain_key", "head_hash", "last_seq", "updated_at"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def head_hash_short(self, obj):
        return obj.head_hash[:16] + "…"
    head_hash_short.short_description = "Head hash"


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ["created_at", "chain_key", "seq", "action", "actor_label", "election"]
    list_filter = ["action"]
    search_fields = ["object_id", "actor_label", "reason"]
    readonly_fields = [f.name for f in AuditEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # Append-only: editing would break the hash chain.
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditCheckpoint)
class AuditCheckpointAdmin(admin.ModelAdmin):
    list_display = ["created_at", "chain_key", "seq", "algorithm", "head_hash_short"]
    readonly_fields = [f.name for f in AuditCheckpoint._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def head_hash_short(self, obj):
        return obj.head_hash[:16] + "…"
    head_hash_short.short_description = "Head hash"
