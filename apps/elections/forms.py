from django import forms

from .models import Election, Position


class ElectionForm(forms.ModelForm):
    class Meta:
        model = Election
        fields = [
            "organization",
            "name",
            "slug",
            "description",
            "election_type",
            "registration_opens",
            "registration_closes",
            "scheduled_opens",
            "scheduled_closes",
            "result_visibility",
            "allow_abstain",
            "min_turnout_pct",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3, "class": "textarea"}),
            "registration_opens": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "registration_closes": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "scheduled_opens": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "scheduled_closes": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "checkbox")
            elif not isinstance(
                field.widget, (forms.DateTimeInput, forms.SelectMultiple)
            ):
                field.widget.attrs.setdefault("class", "input")


class PositionForm(forms.ModelForm):
    class Meta:
        model = Position
        fields = ["name", "slug", "description", "max_choices", "order", "is_active"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2, "class": "textarea"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "checkbox")
            else:
                field.widget.attrs.setdefault("class", "input")
