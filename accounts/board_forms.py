from django import forms

from accounts.models import VerificationMethod


class InviteForm(forms.Form):
    email = forms.EmailField(required=False, help_text="Leave blank to use the address on file.")


class StartResetForm(forms.Form):
    verification_method = forms.ChoiceField(
        label="How did you confirm it is really them?", choices=VerificationMethod.choices
    )
