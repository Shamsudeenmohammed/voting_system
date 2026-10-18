from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

User = get_user_model()


class AdminUserCreationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("username", "email", "display_name", "first_name", "last_name")


class StyledAuthenticationForm(AuthenticationForm):
    """Plain auth form — backend decides credentials; no username enumeration
    differences between unknown-user and wrong-password (Django default)."""

    username = forms.CharField(
        widget=forms.TextInput(
            attrs={"class": "input", "autocomplete": "username", "autofocus": True}
        )
    )
    password = forms.CharField(
        strip=False,
        widget=forms.PasswordInput(
            attrs={"class": "input", "autocomplete": "current-password"}
        ),
    )


class PasswordChangeForm(forms.Form):
    old_password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "input", "autocomplete": "current-password"}),
        strip=False,
    )
    new_password1 = forms.CharField(
        label="New password",
        widget=forms.PasswordInput(attrs={"class": "input", "autocomplete": "new-password"}),
        strip=False,
    )
    new_password2 = forms.CharField(
        label="Confirm new password",
        widget=forms.PasswordInput(attrs={"class": "input", "autocomplete": "new-password"}),
        strip=False,
    )

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_old_password(self):
        if not authenticate(username=self.user.username, password=self.data["old_password"]):
            raise forms.ValidationError("Current password is incorrect.")
        return self.data["old_password"]

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("new_password1"), cleaned.get("new_password2")
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError("Passwords do not match.")
        return cleaned

    def save(self):
        from django.contrib.auth.password_validation import validate_password

        validate_password(self.cleaned_data["new_password1"], self.user)
        self.user.set_password(self.cleaned_data["new_password1"])
        self.user.save(update_fields=["password"])
