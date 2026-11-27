"""Search provider: the "How it works" help pages (D73, core.search).

Same rule as the pages themselves: only topics this person may open
(community.help.may_open), and the whole area leaves search while the help
feature is off (its url name is the Help page). Each page is searched as it
reads today, numbers and the board's own words included.
"""

from django.urls import reverse

from community import help as helpdocs
from community.terms import term_text
from core.search import LIMIT, Result, contains, register, snippet


@register("help", "Help", url_name="help")
def help_pages(user, q):
    out = []
    for topic in helpdocs.topics_for(user):
        text = helpdocs.plain_text(topic)
        if not contains(q, text):
            continue
        out.append(
            Result(
                title=topic.title,
                url=reverse("help_topic", args=[topic.slug]),
                snippet=snippet(q, text.split("\n", 2)[-1], term_text(topic.summary)),
                detail="Help",
            )
        )
    return out[:LIMIT]
