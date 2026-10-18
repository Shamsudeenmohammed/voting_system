from django import forms

from .models import Candidate, Representative


class CandidateForm(forms.ModelForm):
    class Meta:
        model = Candidate
        fields = [
            "position",
            "full_name",
            "slug",
            "manifesto",
            "photo",
            "slate_or_party",
            "order",
        ]
        widgets = {
            "manifesto": forms.Textarea(attrs={"rows": 4, "class": "textarea"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == "photo":
                field.widget.attrs.setdefault("class", "file-input")
            else:
                field.widget.attrs.setdefault("class", "input")


class RepresentativeRegistrationForm(forms.ModelForm):
    class Meta:
        model = Representative
        fields = ["candidate", "note"]
        widgets = {
            "note": forms.Textarea(
                attrs={"rows": 3, "class": "textarea", "placeholder": "Role, contact, credentials"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "input")


class RepresentativeApprovalForm(forms.Form):
    action = forms.ChoiceField(
        choices=[("approve", "Approve"), ("reject", "Reject")],
        widget=forms.Select(attrs={"class": "input"}),
    )
