"""Forms for the organization / people-access console.

Two audiences:

* :class:`OrganizationForm` — creating and editing tenants (organizations).
* :class:`MembershipGrantForm` and :class:`UserCreateForm` — onboarding
  people and assigning the role memberships that drive access.

The views always constrain the ``organization`` choices to the caller's
administrable scope, so a form can never widen a user's own permissions.
"""
from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm

from .models import Organization, OrganizationMembership

User = get_user_model()


class OrganizationForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ["name", "kind", "parent", "description", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input", "placeholder": "e.g. University of Example"}),
            "kind": forms.Select(attrs={"class": "input"}),
            "parent": forms.Select(attrs={"class": "input"}),
            "description": forms.Textarea(attrs={"class": "input", "rows": 2}),
        }

    def __init__(self, *args, allowed_parents=None, **kwargs):
        super().__init__(*args, **kwargs)
        if allowed_parents is not None:
            self.fields["parent"].queryset = allowed_parents
        if self.instance and self.instance.pk:
            # An organization may never be its own parent.
            base = self.fields["parent"].queryset
            self.fields["parent"].queryset = base.exclude(pk=self.instance.pk)
        self.fields["parent"].required = False
        self.fields["parent"].empty_label = "— Top level —"


class MembershipGrantForm(forms.Form):
    """Grant one role membership to a user inside an administrable org."""

    user = forms.ModelChoiceField(
        queryset=User.objects.none(),
        label="User",
        widget=forms.Select(attrs={"class": "input"}),
    )
    organization = forms.ModelChoiceField(
        queryset=Organization.objects.none(),
        label="Organization",
        widget=forms.Select(attrs={"class": "input"}),
    )
    role = forms.ChoiceField(
        choices=OrganizationMembership.ROLE_CHOICES,
        label="Role",
        widget=forms.Select(attrs={"class": "input"}),
    )

    def __init__(self, *args, organizations=None, users=None, allowed_roles=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organizations is not None:
            self.fields["organization"].queryset = organizations
        self.fields["user"].queryset = (users or User.objects.all()).order_by(
            "username"
        )
        if allowed_roles is not None:
            allowed = set(allowed_roles)
            self.fields["role"].choices = [
                (value, label)
                for value, label in OrganizationMembership.ROLE_CHOICES
                if value in allowed
            ]


class UserCreateForm(UserCreationForm):
    """Create a new account and (optionally) assign its first membership."""

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            css = "input"
            if name == "username":
                field.widget.attrs.update({"class": css, "autocomplete": "off"})
            elif name == "email":
                field.widget.attrs.update({"class": css, "type": "email"})
            else:
                field.widget.attrs.update({"class": css})
