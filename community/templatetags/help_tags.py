"""{% how_it_works "slug" %}: the "How this works" link a section page puts
beside its heading (D73). Shows only while help is on and this person may
open that help page."""

from django import template
from django.urls import reverse
from django.utils.html import format_html

register = template.Library()


@register.simple_tag(takes_context=True)
def how_it_works(context, slug: str):
    from community import help as helpdocs

    request = context.get("request")
    if request is None or not request.user.is_authenticated:
        return ""
    topic = helpdocs.BY_SLUG.get(slug)
    if topic is None or not helpdocs.enabled(request):
        return ""
    if not helpdocs.may_open(request.user, topic, request):
        return ""
    from core.templatetags.portal import icon

    return format_html(
        '<a class="how-link" href="{}">{} How this works</a>',
        reverse("help_topic", args=[slug]),
        icon("help"),
    )
