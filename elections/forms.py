from django import forms
from .models import Election, Position, Candidate


class VoterLoginForm(forms.Form):
    voter_id = forms.CharField(
        label="Voter ID",
        widget=forms.TextInput(attrs={"placeholder": "Enter your voter ID"}),
    )
    token = forms.CharField(
        label="Token",
        widget=forms.PasswordInput(attrs={"placeholder": "Enter your token"}),
    )


class VoteForm(forms.Form):
    def __init__(self, positions, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for pos in positions:
            candidates = pos.candidates.order_by("order")
            choices = [(str(c.id), c.full_name) for c in candidates]
            self.fields[f"position_{pos.id}"] = forms.ChoiceField(
                choices=choices,
                label=pos.title,
                widget=forms.RadioSelect,
                required=True,
            )


class ElectionForm(forms.ModelForm):
    class Meta:
        model = Election
        fields = ["title", "description", "category", "ballot_type", "start_at", "end_at", "is_published", "show_results"]
        widgets = {
            "start_at": forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-input"}),
            "end_at": forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-input"}),
            "description": forms.Textarea(attrs={"rows": 3, "class": "form-input"}),
            "title": forms.TextInput(attrs={"class": "form-input"}),
        }

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_at")
        end = cleaned.get("end_at")
        if start and end and start >= end:
            raise forms.ValidationError("End time must be after start time.")
        return cleaned


class PositionForm(forms.ModelForm):
    class Meta:
        model = Position
        fields = ["title", "description", "order"]
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-input"}),
            "description": forms.Textarea(attrs={"rows": 2, "class": "form-input"}),
            "order": forms.NumberInput(attrs={"class": "form-input", "style": "width:80px;"}),
        }


class CandidateForm(forms.ModelForm):
    class Meta:
        model = Candidate
        fields = ["full_name", "bio", "photo", "order"]
        widgets = {
            "full_name": forms.TextInput(attrs={"class": "form-input"}),
            "bio": forms.Textarea(attrs={"rows": 2, "class": "form-input"}),
            "order": forms.NumberInput(attrs={"class": "form-input", "style": "width:80px;"}),
        }


class VoterUploadForm(forms.Form):
    csv_file = forms.FileField(
        label="CSV File",
        help_text="Upload a CSV with columns: voter_id, full_name, email (optional)",
        widget=forms.FileInput(attrs={"accept": ".csv"}),
    )
