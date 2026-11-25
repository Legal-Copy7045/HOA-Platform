from django import forms as djforms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from community import services
from community.models import ContactMethod, ContactMethodKind, ContactProfile, Person
from core import policy


class ProfileForm(djforms.Form):
    phone = djforms.CharField(max_length=40, required=False)
    mailing_address = djforms.CharField(
        required=False,
        widget=djforms.Textarea(attrs={"rows": 3}),
        help_text="Only if different from your lot address.",
    )
    electronic_notice_consent = djforms.BooleanField(
        required=False, label="Send me official association notices by email"
    )
    in_directory = djforms.BooleanField(
        required=False, label="List me in the owner directory (name and lot)"
    )
    directory_show_email = djforms.BooleanField(
        required=False, label="...and show my email address"
    )
    directory_show_phone = djforms.BooleanField(required=False, label="...and show my phone number")
    weekly_digest = djforms.BooleanField(
        required=False,
        label="Send routine updates as one weekly summary",
        help_text="Replies, discussions and sign-off requests. "
        "Official notices always come at once.",
    )


@login_required
def my_profile(request):
    person = Person.objects.for_user(request.user).filter(user=request.user).first()
    profile = (
        ContactProfile.objects.for_user(request.user).filter(person=person).first()
        if person
        else None
    )
    initial = {}
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
