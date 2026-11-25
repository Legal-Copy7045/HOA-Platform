from django import forms
from django.contrib.auth.password_validation import (
    MinimumLengthValidator,
    get_default_password_validators,
)


def password_min_length() -> int:
    """The minimum from AUTH_PASSWORD_VALIDATORS, so the page states the real rule."""
    for validator in get_default_password_validators():
        if isinstance(validator, MinimumLengthValidator):
            return validator.min_length
    return 12


class AcceptInvitationForm(forms.Form):
    password1 = forms.CharField(
        label="Choose a password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        strip=False,
    )
    password2 = forms.CharField(
        label="Type it again",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        strip=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.min_length = password_min_length()
        self.fields["password1"].help_text = (
            f"At least {self.min_length} characters. A few unrelated words work well. "
            "Don't reuse a password from another site."
        )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password1") != cleaned.get("password2"):
            raise forms.ValidationError("The two passwords do not match.")
        return cleaned
