from django.contrib import admin

from .models import OperationalNotice


@admin.register(OperationalNotice)
class OperationalNoticeAdmin(admin.ModelAdmin):
    list_display = ("title", "election", "audience", "is_active", "created_at")
    list_filter = ("audience", "is_active", "election")
