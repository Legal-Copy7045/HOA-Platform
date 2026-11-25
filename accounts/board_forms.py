from django import forms

from accounts.models import VerificationMethod


class InviteForm(forms.Form):
    """No fields: invitations always go to the address on the person's record."""


class StartResetForm(forms.Form):
    verification_method = forms.ChoiceField(
        label="How did you confirm it is really them?", choices=VerificationMethod.choices
    )
