from django import forms

from apps.orgs.models import Organization
from .models import Voter, VoterImportBatch


class VoterLoginForm(forms.Form):
    voter_number = forms.CharField(
        max_length=40,
        label="Voter number / institutional ID",
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "autocomplete": "username",
                "autofocus": True,
                "spellcheck": "false",
            }
        ),
    )
    password = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(
            attrs={"class": "input", "autocomplete": "current-password"}
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.voter = None
        self.password_ok = False
        self.cleaned = {}

    def clean(self):
        cleaned = super().clean()
        number = (cleaned.get("voter_number") or "").strip()
        password = cleaned.get("password") or ""
        self.cleaned = {"password_ok": False}
        if not number or not password:
            return cleaned
        from .models import VoterCredential

        voter = Voter.objects.filter(
            voter_number=number, status=Voter.Status.ACTIVE
        ).select_related("organization").first()
        # Uniform failure path whether the voter number exists or not.
        if voter is None:
            # Constant-ish work to reduce enumeration timing signal.
            VoterCredential(password="").check_password(password)
            raise forms.ValidationError("Invalid voter number or password.")
        cred = VoterCredential.objects.filter(voter=voter).first()
        if cred is None or not cred.password:
            raise forms.ValidationError("Invalid voter number or password.")
        if not cred.check_password(password):
            raise forms.ValidationError("Invalid voter number or password.")
        if not voter.organization.is_active:
            raise forms.ValidationError("This organization is not active.")
        self.voter = voter
        self.password_ok = True
        self.cleaned = {"password_ok": True}
        return cleaned


class VoterImportForm(forms.Form):
    organization = forms.ModelChoiceField(
        queryset=Organization.objects.filter(is_active=True),
        widget=forms.Select(attrs={"class": "input"}),
    )
    file = forms.FileField(
        label="CSV or XLSX file",
        widget=forms.ClearableFileInput(attrs={"class": "file-input", "accept": ".csv,.xlsx"}),
        help_text="Columns: voter_number, full_name (required); email, phone, unit, unit_type (optional).",
    )


class VoterSearchForm(forms.Form):
    q = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": "input", "placeholder": "Search number or name"}),
    )
    status = forms.ChoiceField(
        required=False,
        choices=[("", "Any status")] + Voter.Status.choices,
        widget=forms.Select(attrs={"class": "input"}),
    )
