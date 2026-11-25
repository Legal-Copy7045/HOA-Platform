from django import forms

from community.models import (
    AppointedBy,
    Lot,
    Office,
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
    lot = forms.ModelChoiceField(queryset=Lot.objects.none())
    ownership_type = forms.ChoiceField(choices=OwnershipType.choices)
    start_date = forms.DateField(widget=DateInput, initial=today)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lot"].queryset = Lot.objects.for_user(user)


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
