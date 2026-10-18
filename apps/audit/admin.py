from django.contrib import admin

from .models import AuditCheckpoint, AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("seq", "action", "organization", "actor_repr", "created_at")
    list_filter = ("action", "organization")
    search_fields = ("action", "object_id", "actor_repr")
    readonly_fields = [
        f.name for f in AuditEvent._meta.fields
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditCheckpoint)
class AuditCheckpointAdmin(admin.ModelAdmin):
    list_display = ("organization", "last_seq", "created_at", "created_by")
    readonly_fields = ("last_seq", "head_hash", "signature")
