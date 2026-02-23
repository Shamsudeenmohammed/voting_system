# elections/forms.py
from django import forms
from .models import Election, Ballot, Choice
from django.utils.safestring import mark_safe


class VoterLoginForm(forms.Form):
    voter_id = forms.CharField(label="Student ID / Voter ID")
    token = forms.CharField(label="Password / Token", widget=forms.PasswordInput)

class TokenForm(forms.Form):
    token = forms.CharField(max_length=128, widget=forms.TextInput(attrs={"autocomplete": "off"}))

class SingleChoiceForm(forms.Form):
    # we'll dynamically create fields in the view per ballot
    def __init__(self, ballot: Ballot, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(str(c.id), c.text) for c in ballot.choices.order_by("order")]
        self.fields["choice"] = forms.ChoiceField(
            choices=choices,
            widget=forms.RadioSelect,
            label=mark_safe(ballot.question),
            required=True
        )

class MultipleChoiceForm(forms.Form):
    def __init__(self, ballot: Ballot, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(str(c.id), c.text) for c in ballot.choices.order_by("order")]
        self.fields["choices"] = forms.MultipleChoiceField(
            choices=choices,
            widget=forms.CheckboxSelectMultiple,
            label=mark_safe(ballot.question),
            required=True
        )

# For score/ranked, you'd add specialized forms; keep minimal here.
