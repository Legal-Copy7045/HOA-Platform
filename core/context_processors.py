from django.conf import settings
from django.http import HttpRequest


def site(request: HttpRequest) -> dict[str, str]:
    return {"SITE_NAME": settings.SITE_NAME}


def navigation(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    from accounts import setup

    # During the guided sign-in setup the sidebar stays hidden (UX review #2).
    signin_setup = setup.state(request)
    if signin_setup is not None:
        return {"signin_setup": signin_setup}
    from community.models import Person
    from core import navigation as nav
    from core.scoping import system_access

    data = nav.build(request)
    with system_access("sidebar display name"):
        person = Person.objects.filter(user=request.user).only("display_name").first()
    data["display_name"] = person.display_name if person else request.user.email
    return {"nav": data}


def login_background(request):
    """The sign-in page picture, for pages shown before signing in."""
    if getattr(request, "user", None) is not None and request.user.is_authenticated:
        return {}
    from django.urls import reverse

    from community.branding import current_backgrounds

    current = current_backgrounds()
    desktop, phone = current["desktop"], current["phone"]
    if desktop is None and phone is None:
        return {}

    def url(image):
        return reverse("login_background_image", args=[image.pk]) if image else ""

    # One picture serves both sizes when only one is set.
    return {
        "login_background_url": url(desktop or phone),
        "login_background_phone_url": url(phone or desktop),
    }
