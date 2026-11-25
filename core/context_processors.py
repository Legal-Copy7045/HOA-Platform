from django.conf import settings
from django.http import HttpRequest


def site(request: HttpRequest) -> dict[str, str]:
    return {"SITE_NAME": settings.SITE_NAME}


def navigation(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    from community.models import Person
    from core import navigation as nav
    from core.scoping import system_access

    data = nav.build(request)
    with system_access("sidebar display name"):
        person = Person.objects.filter(user=request.user).only("display_name").first()
    data["display_name"] = person.display_name if person else request.user.email
    return {"nav": data}
