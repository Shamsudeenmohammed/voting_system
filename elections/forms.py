"""Forms for the elections app.

Two audiences:

* Staff forms (election creation, position/candidate configuration, voter
  registry uploads) — always executed inside an org-scoped, role-checked
  view.
* The anonymous voter-access form (voter identifier entry). The view behaves
  identically for existing and non-existing identifiers.

Nothing here ever handles ballot selections: the ballot itself is rendered
and accepted exclusively by the ``ballot`` app.
"""
from __future__ import annotations

from django import forms

from .models import Candidate, Election, PollingStation, Position, Voter

TZ_CHOICES = [
    ("Africa/Accra", "Africa/Accra (GMT)"),
    ("Africa/Lagos", "Africa/Lagos (WAT)"),
    ("Africa/Nairobi", "Africa/Nairobi (EAT)"),
    ("Europe/London", "Europe/London (GMT/BST)"),
    ("Europe/Berlin", "Europe/Berlin (CET/CEST)"),
    ("UTC", "UTC"),
    ("America/New_York", "America/New_York (ET)"),
    ("America/Chicago", "America/Chicago (CT)"),
    ("America/Los_Angeles", "America/Los_Angeles (PT)"),
    ("Asia/Dubai", "Asia/Dubai (GST)"),
    ("Asia/Kolkata", "Asia/Kolkata (IST)"),
    ("Asia/Singapore", "Asia/Singapore (SGT)"),
    ("Australia/Sydney", "Australia/Sydney (AEST)"),
]


class ElectionForm(forms.ModelForm):
    class Meta:
        model = Election
        fields = [
            "title",
            "description",
            "category",
            "ballot_type",
            "start_at",
            "end_at",
            "results_visibility",
            "allow_blank",
        ]
        widgets = {
            "title": forms.TextInput(attrs={"class": "input"}),
            "description": forms.Textarea(attrs={"class": "input", "rows": 3}),
            "category": forms.Select(attrs={"class": "input"}),
            "ballot_type": forms.Select(attrs={"class": "input"}),
            "start_at": forms.DateTimeInput(
                attrs={"class": "input", "type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "end_at": forms.DateTimeInput(
                attrs={"class": "input", "type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "results_visibility": forms.Select(attrs={"class": "input"}),
            "allow_blank": forms.CheckboxInput(attrs={"class": "checkbox"}),
        }

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_at")
        end = cleaned.get("end_at")
        if start and end and end <= start:
            raise forms.ValidationError("Voting must close after it opens.")
        return cleaned


class PositionForm(forms.ModelForm):
    class Meta:
        model = Position
        fields = ["title", "description", "order", "max_choices"]
        widgets = {
            "title": forms.TextInput(attrs={"class": "input"}),
            "description": forms.Textarea(attrs={"class": "input", "rows": 2}),
            "order": forms.NumberInput(attrs={"class": "input"}),
            "max_choices": forms.NumberInput(attrs={"class": "input"}),
        }


class CandidateForm(forms.ModelForm):
    class Meta:
        model = Candidate
        fields = ["full_name", "bio", "manifesto", "order", "status"]
        widgets = {
            "full_name": forms.TextInput(attrs={"class": "input"}),
            "bio": forms.Textarea(attrs={"class": "input", "rows": 2}),
            "manifesto": forms.Textarea(attrs={"class": "input", "rows": 4}),
            "order": forms.NumberInput(attrs={"class": "input"}),
            "status": forms.Select(attrs={"class": "input"}),
        }


class PollingStationForm(forms.ModelForm):
    class Meta:
        model = PollingStation
        fields = ["name", "code", "location", "channel"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input"}),
            "code": forms.TextInput(attrs={"class": "input"}),
            "location": forms.TextInput(attrs={"class": "input"}),
            "channel": forms.Select(attrs={"class": "input"}),
        }


class VoterForm(forms.ModelForm):
    class Meta:
        model = Voter
        fields = ["voter_id", "full_name", "email", "organization_unit"]
        widgets = {
            "voter_id": forms.TextInput(attrs={"class": "input"}),
            "full_name": forms.TextInput(attrs={"class": "input"}),
            "email": forms.EmailInput(attrs={"class": "input"}),
            "organization_unit": forms.TextInput(attrs={"class": "input"}),
        }


class VoterBulkForm(forms.Form):
    """Paste a CSV of voters: ``voter_id,full_name,email,unit`` per line."""

    csv_data = forms.CharField(
        label="Voter records (CSV: voter_id,full_name,email,unit)",
        widget=forms.Textarea(
            attrs={
                "class": "input",
                "rows": 10,
                "placeholder": "V001,Ada Lovelace,ada@example.org,Science Faculty",
            }
        ),
        help_text="One voter per line; name/email/unit are optional.",
    )
    issue_credentials = forms.BooleanField(
        required=False,
        initial=False,
        help_text=(
            "Issue single-use ballot authorizations immediately. Tokens are "
            "shown exactly once for out-of-band delivery and never stored."
        ),
    )

    def clean_csv_data(self):
        raw = self.cleaned_data["csv_data"]
        rows: list[dict] = []
        for lineno, line in enumerate(raw.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            voter_id = parts[0] if parts else ""
            full_name = parts[1] if len(parts) > 1 else ""
            email = parts[2] if len(parts) > 2 else ""
            unit = parts[3] if len(parts) > 3 else ""
            if not voter_id and not full_name:
                raise forms.ValidationError(
                    f"Line {lineno}: a voter_id or full name is required."
                )
            rows.append(
                {
                    "voter_id": voter_id,
                    "full_name": full_name,
                    "email": email or None,
                    "organization_unit": unit,
                }
            )
        if not rows:
            raise forms.ValidationError("No voter rows found.")
        if len(rows) > 5000:
            raise forms.ValidationError("Bulk import is limited to 5,000 rows per request.")
        self.rows = rows
        return raw


class ApprovalRequestForm(forms.Form):
    """Documented reason for a four-eyes decision (reopen/decertify/config)."""

    reason = forms.CharField(
        label="Documented justification",
        widget=forms.Textarea(attrs={"class": "input", "rows": 3}),
        help_text="Stored permanently in the audit trail; mandatory.",
    )


class ElectionSearchForm(forms.Form):
    q = forms.CharField(
        required=False,
        label="Search",
        widget=forms.TextInput(attrs={"class": "input", "placeholder": "Election title"}),
    )
    state = forms.ChoiceField(
        required=False,
        choices=[("", "All states")] + list(Election.STATE_CHOICES),
        widget=forms.Select(attrs={"class": "input"}),
    )


class VoterAccessForm(forms.Form):
    """Anonymous voter access: enter the voter identifier for this election."""

    voter_id = forms.CharField(
        label="Voter ID",
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": "input", "placeholder": "e.g. V001", "autocomplete": "off"}
        ),
    )
