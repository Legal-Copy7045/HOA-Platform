from django import forms as djforms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from community import services
from community.models import ContactMethod, ContactMethodKind, ContactProfile, Person
from core import policy


def _described(field_name: str) -> dict:
    return {"aria-describedby": f"id_{field_name}_help"}


class ProfileForm(djforms.Form):
    """The owner's own details, grouped on the page as How to reach you, Official
    notices, Owner directory and Email updates (UX review)."""

    phone = djforms.CharField(
        max_length=40, required=False, widget=djforms.TextInput(attrs={"autocomplete": "tel"})
    )
    mailing_address = djforms.CharField(
        required=False,
        widget=djforms.Textarea(attrs={"rows": 3, **_described("mailing_address")}),
        help_text="Only if different from your home address.",
    )
    electronic_notice_consent = djforms.BooleanField(
        required=False,
        label="Send me official association notices by email",
        widget=djforms.CheckboxInput(attrs=_described("electronic_notice_consent")),
        help_text="Notices the association must send you, such as meeting and assessment "
        "notices, then come by email instead of by mail. You can change this at any time.",
    )
    in_directory = djforms.BooleanField(
        required=False,
        label="List me in the owner directory (name and address)",
        widget=djforms.CheckboxInput(attrs=_described("in_directory")),
        help_text="Leave this unticked to stay out of the directory.",
    )
    directory_show_email = djforms.BooleanField(required=False, label="My email address")
    directory_show_phone = djforms.BooleanField(required=False, label="My phone number")
    weekly_digest = djforms.BooleanField(
        required=False,
        label="Send routine updates as one weekly summary",
        widget=djforms.CheckboxInput(attrs=_described("weekly_digest")),
        help_text="Replies, discussions and acknowledgement requests. "
        "Official notices always come at once.",
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


@login_required
def my_profile(request):
    person = Person.objects.for_user(request.user).filter(user=request.user).first()
    profile = (
        ContactProfile.objects.for_user(request.user).filter(person=person).first()
        if person
        else None
    )
    initial = {"statement_emails": True}  # the default until a profile is saved (D42)
    if profile:
        initial = {f: getattr(profile, f) for f in ProfileForm.base_fields}
    form = ProfileForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_my_profile(user=request.user, request=request, **form.cleaned_data)
        except services.CommunityError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Profile saved.")
        return redirect("my_profile")
    methods = ContactMethod.objects.for_user(request.user).filter(person=person) if person else []
    return render(
        request,
        "community/my_profile.html",
        {
            "form": form,
            "person": person,
            "methods": methods,
            "method_kinds": ContactMethodKind.choices,
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
