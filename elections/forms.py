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

from . import governance, lifecycle
from .models import Candidate, Election, PollingStation, Position, Voter
from .photos import validate_candidate_photo

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
            "token_format",
            "token_expiry_hours",
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
            "token_format": forms.Select(attrs={"class": "input"}),
            "token_expiry_hours": forms.NumberInput(attrs={"class": "input", "min": "0"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Newer fields carry model-level defaults; allow them to be omitted by
        # older clients/tests while still defaulting sensibly.
        for name in ("token_format", "token_expiry_hours"):
            if name in self.fields:
                self.fields[name].required = False
                if self.fields[name].initial is None:
                    self.fields[name].initial = self._meta.model._meta.get_field(name).default

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
    """Create/edit form for a candidate, including the passport photograph.

    ``photo`` is declared explicitly (rather than auto-generated from the
    model field) so that the shared server-side validator in
    ``elections.photos`` produces the user-facing messages for every kind of
    rejection (wrong format, corrupted file, too large) instead of Django's
    generic "Upload a valid image" text.  ``Candidate.clean()`` re-applies the
    same validator as a backstop for saves that bypass this form (e.g. admin).
    """

    photo = forms.FileField(
        label="Passport photograph",
        required=False,
        widget=forms.ClearableFileInput(
            attrs={
                "class": "candidate-photo-input",
                "accept": "image/jpeg,image/png,image/webp",
                "data-photo-input": "1",
            }
        ),
        help_text=(
            "JPEG, PNG or WebP. Shown on the official ballot next to the "
            "candidate's name; initials are displayed when empty."
        ),
    )

    class Meta:
        model = Candidate
        fields = ["full_name", "photo", "bio", "manifesto", "order", "status"]
        widgets = {
            "full_name": forms.TextInput(attrs={"class": "input"}),
            "bio": forms.Textarea(attrs={"class": "input", "rows": 2}),
            "manifesto": forms.Textarea(attrs={"class": "input", "rows": 4}),
            "order": forms.NumberInput(attrs={"class": "input"}),
            "status": forms.Select(attrs={"class": "input"}),
        }

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if photo:
            return validate_candidate_photo(photo)
        return photo


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


class VoterFileUploadForm(forms.Form):
    """Upload a register file (CSV/TXT/XLSX) using the downloadable template."""

    file = forms.FileField(
        label="Register file (CSV or XLSX)",
        widget=forms.FileInput(
            attrs={"class": "input", "accept": ".csv,.txt,.xlsx"}
        ),
        help_text=(
            "Columns: voter_id, full_name, email, unit. "
            "Download the template below and keep its header row. "
            "Maximum 5,000 rows per upload."
        ),
    )
    issue_credentials = forms.BooleanField(
        required=False,
        initial=False,
        help_text=(
            "Issue single-use ballot authorizations immediately. Tokens are "
            "shown exactly once for out-of-band delivery and never stored."
        ),
    )


class ApprovalRequestForm(forms.Form):
    """Documented reason for a four-eyes decision (reopen/decertify/config)."""

    reason = forms.CharField(
        label="Documented justification",
        widget=forms.Textarea(attrs={"class": "input", "rows": 3}),
        help_text="Stored permanently in the audit trail; mandatory.",
    )


class ElectionGovernanceForm(forms.Form):
    """Per-election governance policy (persisted in ``Election.rules``).

    This is *data*, never taken from a request parameter for enforcement: the
    view only lets a Super Admin / institution administrator change it while
    the election is in DRAFT, and the value is read back server-side by
    :mod:`elections.governance` for every certify/publish/dual-control check.
    """

    certify_authority = forms.ChoiceField(
        label="Who may certify results",
        choices=governance.CERTIFY_AUTHORITY_CHOICES,
        widget=forms.Select(attrs={"class": "input"}),
    )
    publish_authority = forms.ChoiceField(
        label="Who may publish results",
        choices=governance.CERTIFY_AUTHORITY_CHOICES,
        widget=forms.Select(attrs={"class": "input"}),
    )
    require_institution_approval = forms.BooleanField(
        label="Require a second approver (four-eyes) for sensitive transitions",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "checkbox"}),
    )
    dual_approve_config = forms.BooleanField(
        label="Configuration approval",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "checkbox"}),
    )
    dual_reopen = forms.BooleanField(
        label="Reopening a closed election",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "checkbox"}),
    )
    dual_decertify = forms.BooleanField(
        label="Corrections / decertifying results",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "checkbox"}),
    )

    def policy(self) -> dict:
        data = self.cleaned_data
        require = bool(data.get("require_institution_approval"))
        return {
            "certify_authority": data.get("certify_authority") or "manage",
            "publish_authority": data.get("publish_authority") or "manage",
            "require_institution_approval": require,
            "dual_control": {
                "approve_config": require and bool(data.get("dual_approve_config")),
                "reopen": require and bool(data.get("dual_reopen")),
                "decertify": require and bool(data.get("dual_decertify")),
            },
        }

    @classmethod
    def initial_from(cls, election) -> dict:
        policy = governance.policy_for(election)
        dual = policy["dual_control"]
        return {
            "certify_authority": policy["certify_authority"],
            "publish_authority": policy["publish_authority"],
            "require_institution_approval": policy["require_institution_approval"],
            "dual_approve_config": dual.get("approve_config", True),
            "dual_reopen": dual.get("reopen", True),
            "dual_decertify": dual.get("decertify", True),
        }


DELETE_CONFIRM_TOKEN = "DELETE"

class OverrideForm(forms.Form):
    """Super Admin emergency override: explicit, confirmed and audited."""

    target = forms.ChoiceField(
        label="Force transition to",
        choices=[],
        widget=forms.Select(attrs={"class": "input"}),
    )
    reason = forms.CharField(
        label="Documented reason",
        widget=forms.Textarea(
            attrs={
                "class": "input",
                "rows": 3,
                "placeholder": "Explain why the normal approval path cannot be used…",
            }
        ),
        help_text=(
            "Stored permanently in the audit chain together with the "
            "superseded control and its consequences."
        ),
    )
    confirm = forms.CharField(
        label=f"Type {lifecycle.OVERRIDE_CONFIRM_TOKEN} to confirm",
        max_length=32,
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "autocomplete": "off",
                "placeholder": lifecycle.OVERRIDE_CONFIRM_TOKEN,
            }
        ),
    )

    def __init__(self, *args, election=None, **kwargs):
        super().__init__(*args, **kwargs)
        targets = []
        if election is not None:
            labels = dict(Election.STATE_CHOICES)
            targets = [
                (target, labels.get(target, target.title()))
                for target in sorted(lifecycle.TRANSITIONS.get(election.state, set()))
            ]
        self.fields["target"].choices = targets

    def clean_reason(self):
        reason = (self.cleaned_data.get("reason") or "").strip()
        if len(reason) < lifecycle.MIN_OVERRIDE_REASON:
            raise forms.ValidationError(
                "An emergency override requires a documented reason of at least "
                f"{lifecycle.MIN_OVERRIDE_REASON} characters."
            )
        return reason


class ElectionDeleteForm(forms.Form):
    """Super Admin permanent election deletion with explicit confirmation."""

    reason = forms.CharField(
        label="Documented reason",
        widget=forms.Textarea(
            attrs={
                "class": "input",
                "rows": 3,
                "placeholder": "Explain why permanent deletion is necessary…",
            }
        ),
        help_text="Stored permanently in the audit trail; mandatory.",
    )
    confirm = forms.CharField(
        label=f"Type {DELETE_CONFIRM_TOKEN} to confirm",
        max_length=32,
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "autocomplete": "off",
                "placeholder": DELETE_CONFIRM_TOKEN,
            }
        ),
    )

    def clean_reason(self):
        reason = (self.cleaned_data.get("reason") or "").strip()
        if len(reason) < 10:
            raise forms.ValidationError(
                "Permanent deletion requires a documented reason of at least 10 characters."
            )
        return reason

    def clean_confirm(self):
        confirm = (self.cleaned_data.get("confirm") or "").strip().upper()
        if confirm != DELETE_CONFIRM_TOKEN:
            raise forms.ValidationError(f"Type {DELETE_CONFIRM_TOKEN} to confirm deletion.")
        return confirm


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
