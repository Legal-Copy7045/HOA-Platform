from django import forms
from django.contrib.auth.password_validation import password_validators_help_texts


class AcceptInvitationForm(forms.Form):
    password1 = forms.CharField(
        label="Choose a password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        strip=False,
        help_text=" ".join(password_validators_help_texts()),
    )
    password2 = forms.CharField(
        label="Type it again",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        strip=False,
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password1") != cleaned.get("password2"):
            raise forms.ValidationError("The two passwords do not match.")
        return cleaned
