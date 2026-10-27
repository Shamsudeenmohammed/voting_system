from django import forms
from django.contrib import admin

from .models import Workstation


class WorkstationAdminForm(forms.ModelForm):
    credential = forms.CharField(required=False, widget=forms.PasswordInput(render_value=False))

    class Meta:
        model = Workstation
        fields = "__all__"


@admin.register(Workstation)
class WorkstationAdmin(admin.ModelAdmin):
    form = WorkstationAdminForm
    list_display = ("name", "code", "purpose", "status", "election", "polling_station", "created_at")
    list_filter = ("purpose", "status")
    search_fields = ("name", "code")
    readonly_fields = ("credential_hash", "credential_rotated_at", "created_at", "updated_at", "last_seen_at")

    def save_model(self, request, obj, form, change):
        raw = form.cleaned_data.get("credential") if hasattr(form, "cleaned_data") else None
        if raw:
            obj.set_credential(raw)
        if not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)