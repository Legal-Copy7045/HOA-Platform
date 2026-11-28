from django import forms

from community.home_picker import HomeField
from community.models import (
    AppointedBy,
    Lot,
    Office,
    Ownership,
    OwnershipType,
    Person,
    Role,
    SeatEndReason,
    SeatType,
    today,
)


class DateInput(forms.DateInput):
    input_type = "date"


def _people(user):
    return Person.objects.for_user(user).order_by("display_name")


class PersonForm(forms.Form):
    display_name = forms.CharField(label="Name", max_length=150)
    email = forms.EmailField(label="Email (for the invitation)", required=False)


class OwnershipForm(forms.Form):
    person = forms.ModelChoiceField(queryset=Person.objects.none())
    ownership_type = forms.ChoiceField(choices=OwnershipType.choices)
    start_date = forms.DateField(widget=DateInput, initial=today)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["person"].queryset = _people(user)


class LotOwnershipForm(forms.Form):
    lot = HomeField(label="Home")  # picked by street address (D96)
    ownership_type = forms.ChoiceField(choices=OwnershipType.choices)
    start_date = forms.DateField(widget=DateInput, initial=today)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lot"].queryset = Lot.objects.for_user(user).order_by(
            "street_address", "number"
        )


class EndDateForm(forms.Form):
    end_date = forms.DateField(
        widget=DateInput, initial=today, help_text="Access ends at the start of this day."
    )


class TransferForm(forms.Form):
    new_owners = forms.ModelMultipleChoiceField(queryset=Person.objects.none())
    transfer_date = forms.DateField(widget=DateInput, initial=today)
    ownership_type = forms.ChoiceField(
        choices=[("", "Automatic (sole or joint)"), *OwnershipType.choices], required=False
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["new_owners"].queryset = _people(user)


class SeatForm(forms.Form):
    seat_type = forms.ChoiceField(choices=SeatType.choices)
    term_start = forms.DateField(widget=DateInput, initial=today)
    term_end = forms.DateField(widget=DateInput, required=False)
    citation = forms.CharField(max_length=200, required=False, help_text='e.g. "Bylaws §3.2"')


class EndSeatForm(forms.Form):
    reason = forms.ChoiceField(choices=SeatEndReason.choices)
    ended_on = forms.DateField(widget=DateInput, initial=today)


class OfficeForm(forms.Form):
    office = forms.ChoiceField(choices=Office.choices)
    start_date = forms.DateField(widget=DateInput, initial=today)


class RoleForm(forms.Form):
    role = forms.ChoiceField(
        choices=[
            (Role.ARC_REVIEWER, Role.ARC_REVIEWER.label),
            (Role.AUDITOR, Role.AUDITOR.label),
            (Role.MANAGER, Role.MANAGER.label),
        ]
    )
    start_date = forms.DateField(widget=DateInput, initial=today)
    appointed_by = forms.ChoiceField(
        choices=[("", "-"), *AppointedBy.choices],
        required=False,
        help_text="Required for ARC members.",
    )
    citation = forms.CharField(max_length=200, required=False)


class ProfileForm(forms.Form):
    legal_name = forms.CharField(max_length=200)
    fiscal_year_start_month = forms.IntegerField(min_value=1, max_value=12)
    welcome_message = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Shown at the top of everyone's dashboard. Plain text; leave empty for none.",
    )
    arc_form_return_to = forms.CharField(
        required=False,
        max_length=300,
        label="Alteration form: where to return it",
        help_text="Printed at the top of the alteration request form, e.g. the management "
        "office's address and email.",
    )
    signin_help_contact = forms.CharField(
        required=False,
        max_length=200,
        label="Sign-in help: who owners contact",
        help_text="For a lost password, phone or passkey, e.g. the board's email and phone. "
        "Shown on the sign-in help page, which anyone can open: use association contacts, "
        "not a personal number.",
    )
    arc_form_conditions = forms.CharField(
        required=False,
        max_length=3000,
        label="Alteration form: conditions owners agree to",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Leave empty for the standard wording.",
    )


class RuleForm(forms.Form):
    key = forms.CharField(max_length=64, widget=forms.HiddenInput)
    value = forms.CharField(max_length=200, required=False)
    citation = forms.CharField(max_length=200, required=False)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class CommonAreaForm(forms.Form):
    from community.models import CommonAreaKind

    name = forms.CharField(max_length=120)
    kind = forms.ChoiceField(choices=CommonAreaKind.choices)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


# --- Owners ask the board to change who is on their account (D41) -------------------

EMAIL_LABEL = "Their email (they sign in with this)"
NO_LOGIN_LABEL = "They don't use email: keep them on the account without a sign-in"
NO_LOGIN_HELP = (
    "Without an email they cannot sign in until the board adds one to their record and"
    " invites them."
)


class _AddedPersonForm(forms.Form):
    name = forms.CharField(label="Their name", max_length=200)
    email = forms.EmailField(label=EMAIL_LABEL, required=False, max_length=254)
    no_login = forms.BooleanField(label=NO_LOGIN_LABEL, required=False, help_text=NO_LOGIN_HELP)

    def clean(self):
        data = super().clean()
        if not data.get("email") and not data.get("no_login") and "email" not in self.errors:
            self.add_error("email", "Enter their email, or tick that they don't use email.")
        return data


class ContactAddRequestForm(_AddedPersonForm):
    from community.models import ContactKind

    kind = forms.ChoiceField(label="What they may do", choices=ContactKind.choices)
    reason = forms.CharField(label="Reason (optional)", required=False, max_length=500)


DEED_HELP = "A scan or photo of the deed as recorded (PDF, JPG or PNG)."


class OwnerAddRequestForm(_AddedPersonForm):
    from community.models import ADDED_OWNER_TYPES

    ownership_type = forms.ChoiceField(
        label="How they hold the deed",
        choices=[(t.value, t.label) for t in ADDED_OWNER_TYPES],
    )
    deed_date = forms.DateField(
        label="Date on the recorded deed", widget=DateInput, help_text="Not in the future."
    )
    deed = forms.FileField(label="The recorded deed", help_text=DEED_HELP)
    reason = forms.CharField(
        label="Anything the board should know (optional)", required=False, max_length=500
    )

    def clean_deed_date(self):
        value = self.cleaned_data["deed_date"]
        if value and value > today():
            raise forms.ValidationError("The deed date cannot be in the future.")
        return value


class OwnerRemoveRequestForm(forms.Form):
    ownership = forms.ModelChoiceField(
        label="Owner to remove", queryset=Ownership.objects.none(), empty_label=None
    )
    deed_date = forms.DateField(
        label="Date on the recorded deed", widget=DateInput, help_text="Not in the future."
    )
    deed = forms.FileField(
        label="The recorded deed",
        help_text="The deed or other recorded document that removes them (for example after a"
        " death or a quitclaim). " + DEED_HELP,
    )
    reason = forms.CharField(
        label="Anything the board should know (optional)", required=False, max_length=500
    )

    def __init__(self, *args, ownerships, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["ownership"].queryset = ownerships
        self.fields["ownership"].label_from_instance = lambda o: o.person.display_name

    def clean_deed_date(self):
        value = self.cleaned_data["deed_date"]
        if value and value > today():
            raise forms.ValidationError("The deed date cannot be in the future.")
        return value
