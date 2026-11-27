"""Template tags for the vendor onboarding links on the shared vendor pages (D79)."""

from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def vendor_onboarding_on(context) -> bool:
    from core import features, policy

    request = context.get("request")
    user = getattr(request, "user", None)
    return bool(
        user is not None
        and user.is_authenticated
        and features.enabled("vendor_onboarding", request)
        and policy.can(user, "vendor.onboarding_review")
    )


@register.simple_tag
def vendor_status(vendor) -> str:
    from operations.vendor_onboarding import status_text

    return status_text(vendor)
