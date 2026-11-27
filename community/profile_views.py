from django import forms as djforms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from community import notices, services
from community.models import (
    ContactMethod,
    ContactMethodKind,
    ContactProfile,
    Ownership,
    Person,
    PhoneType,
)
from core import policy


def _described(field_name: str) -> dict:
    return {"aria-describedby": f"id_{field_name}_help"}


class ProfileForm(djforms.Form):
    """Directory and routine-update choices (the contact details have their
    own forms in the "How we reach you" card, D44)."""

    in_directory = djforms.BooleanField(
        required=False,
        label="List me in the owner directory (name and address)",
        widget=djforms.CheckboxInput(attrs=_described("in_directory")),
        help_text="Leave this unticked to stay out of the directory.",
    )
    directory_show_email = djforms.BooleanField(required=False, label="My email address")
    directory_show_phone = djforms.BooleanField(
        required=False, label="My phone number (the first on my Phone list)"
    )
    weekly_digest = djforms.TypedChoiceField(
        label="Routine updates (replies, discussions, requests to you)",
        choices=[
            ("now", "One email per update"),
            ("weekly", "A weekly summary, only sent in weeks when there is something new"),
        ],
        coerce=lambda v: v == "weekly",
        widget=djforms.RadioSelect(attrs=_described("weekly_digest")),
        help_text="Official notices always come straight away.",
    )
    # D42: on by default (owner ruling); the label names the board's word.
    statement_emails = djforms.BooleanField(
        required=False,
        label="Email me a statement when the monthly {term} is charged",
        widget=djforms.CheckboxInput(attrs=_described("statement_emails")),
        help_text="The amount due, a link to your account and your statement (PDF). "
        "Untick to stop these emails.",
    )

    def __init__(self, *args, **kwargs):
        from community.terms import term_text

        super().__init__(*args, **kwargs)
        field = self.fields["statement_emails"]
        field.label = term_text(field.label)


class NoticeChoiceForm(djforms.Form):
    choice = djforms.ChoiceField(
        choices=[("email", notices.YES_LABEL), ("post", notices.NO_LABEL)],
        error_messages={"required": "Choose email or post."},
    )


class EmailAddForm(djforms.Form):
    email = djforms.EmailField(
        label="Email address",
        max_length=254,
        widget=djforms.EmailInput(attrs={"autocomplete": "email"}),
    )
    label = djforms.CharField(
        label="Label (optional)",
        max_length=60,
        required=False,
        widget=djforms.TextInput(attrs={"placeholder": "Work, spouse..."}),
    )
    notices = djforms.BooleanField(required=False, label="Also send official notices here")


class PhoneAddForm(djforms.Form):
    phone_type = djforms.ChoiceField(label="Type", choices=PhoneType.choices)
    number = djforms.CharField(
        label="Phone number",
        max_length=40,
        widget=djforms.TextInput(attrs={"type": "tel", "autocomplete": "tel"}),
    )
    label = djforms.CharField(label="Label (optional)", max_length=60, required=False)


STATE_CHOICES = [(s, s) for s in services.US_STATES]


class MailingForm(djforms.Form):
    where = djforms.ChoiceField(
        label="Send post to",
        choices=[
            ("home", "Same as my home's address"),
            ("other", "A different address (for example if you don't live there)"),
        ],
        widget=djforms.RadioSelect,
        initial="home",
    )
    street = djforms.CharField(
        label="Street address",
        max_length=200,
        required=False,
        widget=djforms.TextInput(attrs={"autocomplete": "address-line1"}),
    )
    unit = djforms.CharField(
        label="Apartment, suite or unit (optional)",
        max_length=60,
        required=False,
        widget=djforms.TextInput(attrs={"autocomplete": "address-line2"}),
    )
    city = djforms.CharField(
        label="City or town",
        max_length=80,
        required=False,
        widget=djforms.TextInput(attrs={"autocomplete": "address-level2"}),
    )
    state = djforms.ChoiceField(label="State", choices=STATE_CHOICES, initial="PA")
    zip_code = djforms.RegexField(
        label="ZIP code",
        regex=r"^[0-9]{5}(-?[0-9]{4})?$",
        required=False,
        max_length=10,
        error_messages={"invalid": "Enter a 5-digit ZIP code (or ZIP+4, like 19380-1234)."},
        widget=djforms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "postal-code"}),
    )

    def clean(self):
        data = super().clean()
        if data.get("where") == "other":
            for name, message in (
                ("street", "Enter the street address."),
                ("city", "Enter the city or town."),
                ("zip_code", "Enter the ZIP code."),
            ):
                if not data.get(name) and name not in self.errors:
                    self.add_error(name, message)
        return data


SECTIONS = ("notices", "email_add", "phone_add", "mailing", "prefs")


def _is_owner(person) -> bool:
    if person is None:
        return False
    from core.scoping import system_access

    with system_access("my profile: owner on the deed"):
        return Ownership.objects.active().filter(person=person).exists()


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def my_profile(request):
    user = request.user
    person = Person.objects.for_user(user).filter(user=user).first()
    profile = (
        ContactProfile.objects.for_user(user).filter(person=person).first() if person else None
    )
    initial = {"statement_emails": True}  # the default until a profile is saved (D42)
    if profile:
        initial = {f: getattr(profile, f) for f in ProfileForm.base_fields}
        initial["weekly_digest"] = "weekly" if profile.weekly_digest else "now"
    mailing_initial = {"where": "home", "state": "PA"}
    if profile and profile.mail_to_home is False:
        mailing_initial = {
            "where": "other",
            "street": profile.mail_street,
            "unit": profile.mail_unit,
            "city": profile.mail_city,
            "state": profile.mail_state or "PA",
            "zip_code": profile.mail_zip,
        }
    section = request.POST.get("section", "prefs") if request.method == "POST" else ""
    if section and section not in SECTIONS:
        section = "prefs"
    posted = request.POST if request.method == "POST" else None
    forms = {
        "notices": NoticeChoiceForm(posted if section == "notices" else None),
        "email_add": EmailAddForm(posted if section == "email_add" else None),
        "phone_add": PhoneAddForm(posted if section == "phone_add" else None),
        "mailing": MailingForm(posted if section == "mailing" else None, initial=mailing_initial),
        "prefs": ProfileForm(posted if section == "prefs" else None, initial=initial),
    }
    if section:
        form = forms[section]
        if form.is_valid():
            try:
                ok, anchor = _save(request, section, form.cleaned_data, person)
            except services.CommunityError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, ok)
                return redirect(reverse("my_profile") + anchor)
    return render(request, "community/my_profile.html", _context(request, person, profile, forms))


def _save(request, section: str, data: dict, person) -> tuple[str, str]:
    user = request.user
    if section == "notices":
        if not _is_owner(person):
            raise services.CommunityError("Official notices go to the owners on the deed.")
        services.record_notice_choice(
            user=user, consent=data["choice"] == "email", via="profile", request=request
        )
        return "Your choice for official notices is saved.", "#notices"
    if section == "email_add":
        if person is None:
            raise services.CommunityError("Your login is not linked to a person yet.")
        services.add_contact_method(
            person,
            kind=ContactMethodKind.EMAIL,
            value=data["email"],
            label=data["label"],
            notices=data["notices"],
            actor=user,
            request=request,
        )
        return "Email added.", "#email"
    if section == "phone_add":
        if person is None:
            raise services.CommunityError("Your login is not linked to a person yet.")
        services.add_contact_method(
            person,
            kind=ContactMethodKind.PHONE,
            value=data["number"],
            label=data["label"],
            phone_type=data["phone_type"],
            actor=user,
            request=request,
        )
        return "Phone added.", "#phone"
    if section == "mailing":
        services.set_mailing_address(
            user=user,
            to_home=data["where"] == "home",
            street=data["street"],
            unit=data["unit"],
            city=data["city"],
            state=data["state"],
            zip_code=data["zip_code"],
            request=request,
        )
        return "Mailing address saved.", "#mailing"
    services.update_my_profile(user=user, request=request, **data)
    return "Profile saved.", "#choices"


def _context(request, person, profile, forms) -> dict:
    user = request.user
    methods = list(ContactMethod.objects.for_user(user).filter(person=person)) if person else []
    owner = _is_owner(person)
    home_address, source = services.postal_address(person) if person else ("", "none")
    old = services.old_address(person.pk, profile) if person else ""
    return {
        "forms": forms,
        "form": forms["prefs"],
        "person": person,
        "profile": profile,
        "is_owner": owner,
        "emails": [m for m in methods if m.kind == ContactMethodKind.EMAIL],
        "phones": [m for m in methods if m.kind == ContactMethodKind.PHONE],
        "old_methods": [m for m in methods if m.kind == ContactMethodKind.ADDRESS],
        "consent": bool(profile and profile.electronic_notice_consent),
        "answered": bool(profile and profile.consent_changed_at),
        "notice_kinds": notices.NOTICE_KINDS,
        "notice_text": notices.CONSENT_TEXT,
        "yes_label": notices.YES_LABEL,
        "no_label": notices.NO_LABEL,
        "post_address": home_address,
        "post_source": source,
        "old_address": old,
        "address_check": bool(person) and services.needs_address_check(person.pk),
        "home_address": _home_address(person),
        "bouncing": _bouncing(person),
    }


def _bouncing(person) -> list[dict]:
    """The person's own addresses that bounce (D74)."""
    from comms import bounces

    return bounces.bouncing(person) if person and bounces.feature_on() else []


def _home_address(person) -> str:
    if person is None:
        return ""
    from core.scoping import system_access

    with system_access("my profile: home address"):
        own = Ownership.objects.active().filter(person=person).select_related("lot").first()
    return own.lot.street_address if own else ""


# --- Official notices step (D44) ----------------------------------------------------------


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def notice_choice(request):
    """The one-time "Official notices" step after sign-up (and the page the
    My items to-do opens). Only for current owners on the deed, only once a
    passkey or authenticator app is set up (the MFA middleware sends anyone
    without one to enrollment first; this check repeats it), and only for the
    signed-in person: there is no id in the URL."""
    from accounts.auth import has_strong_authenticator

    user = request.user
    if not has_strong_authenticator(user):
        return redirect("mfa_index")
    person = Person.objects.for_user(user).filter(user=user).first()
    if not _is_owner(person) or not policy.roles_for(user).owned_lot_ids:
        raise Http404
    profile = ContactProfile.objects.for_user(user).filter(person=person).first()
    answered = bool(profile and profile.consent_changed_at)
    if request.method == "GET" and request.GET.get("chosen") == "post" and answered:
        address, _source = services.postal_address(person)
        return render(
            request, "community/notice_choice_done.html", {"address": address, "person": person}
        )
    if request.method == "GET" and answered:
        # Answered already (here or on My profile): never asked again.
        return redirect(reverse("my_profile") + "#notices")
    form = NoticeChoiceForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            services.record_notice_choice(
                user=user,
                consent=form.cleaned_data["choice"] == "email",
                via="signup",
                request=request,
            )
        except services.CommunityError as exc:
            form.add_error(None, str(exc))
        else:
            if form.cleaned_data["choice"] == "email":
                messages.success(request, "Official notices will come to you by email.")
                return redirect("home")
            return redirect(reverse("notice_choice") + "?chosen=post")
    return render(
        request,
        "community/notice_choice.html",
        {
            "form": form,
            "notice_kinds": notices.NOTICE_KINDS,
            "notice_text": notices.CONSENT_TEXT,
            "yes_label": notices.YES_LABEL,
            "no_label": notices.NO_LABEL,
        },
    )


@login_required
def directory(request):
    policy.require(request.user, "directory.view")
    tab = request.GET.get("tab", "owners")
    context = {"tab": tab}
    if tab == "board":
        context["board"] = services.board_roster_public()
    elif tab == "committees":
        from governance.services import committee_directory

        context["committees"] = committee_directory()
    else:
        context["tab"] = "owners"
        context["entries"] = services.directory_entries()
    return render(request, "community/directory.html", context)
