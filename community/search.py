"""Search providers: governing rules, lots, people and the owner directory
(core.search). Each uses the same permission and data as its page.

* Rules, lots and people are board-side pages (community_settings,
  lot_list, people); owners never see them in search.
* Owners find other owners only through the opt-in directory, and only by
  the fields each owner chose to share (services.directory_entries).
"""

from django.urls import reverse

from core import policy
from core.search import LIMIT, Result, contains, matching, register, snippet


@register("rules", "Community rules", url_name="community_settings")
def rules(user, q):
    from community.models import GoverningRule
    from community.rules import REGISTRY, group_title
    from community.terms import charge_term, term_text

    if not policy.can(user, "lot.view_all"):
        return []
    rows = {r.key: r for r in GoverningRule.objects.all()}
    term = charge_term()
    out = []
    for spec in REGISTRY.values():
        row = rows.get(spec.key)
        citation = row.citation if row else ""
        notes = row.notes if row else ""
        label, help_text = term_text(spec.label, term), term_text(spec.help, term)
        group = group_title(spec.group, term)
        if not contains(q, label, help_text, group, citation, notes):
            continue
        out.append(
            Result(
                title=label,
                url=reverse("community_settings"),
                snippet=snippet(q, help_text, citation, notes),
                date=row.updated_at if row else None,
                detail=group + (f" · {citation}" if citation else ""),
            )
        )
    return out[:LIMIT]


@register("lots", "Homes", url_name="lot_list")
def lots(user, q):
    from django.db.models import Q

    from community.models import Lot, Ownership

    if not policy.can(user, "lot.view_all"):
        return []
    owners: dict[int, list[str]] = {}
    for o in (
        Ownership.objects.for_user(user)
        .active()
        .filter(person__display_name__icontains=q)
        .select_related("person")
    ):
        owners.setdefault(o.lot_id, []).append(o.person.display_name)
    condition = matching(q, "street_address") | Q(pk__in=list(owners))
    if q.isdigit() and len(q) <= 5:
        condition |= Q(number=int(q))
    rows = sorted(Lot.objects.for_user(user).filter(condition), key=lambda lot: lot.sort_key)[
        :LIMIT
    ]
    return [
        Result(
            title=lot.label,
            url=reverse("lot_detail", args=[lot.pk]),
            snippet=snippet(q, lot.street_address, ", ".join(owners.get(lot.pk, []))),
        )
        for lot in rows
    ]


@register("people", "People", url_name="people")
def people(user, q):
    from community.models import Person

    if not policy.can(user, "person.manage"):
        return []
    rows = (
        Person.objects.for_user(user)
        .filter(matching(q, "display_name", "email"))
        .order_by("-created_at")[:LIMIT]
    )
    return [
        Result(
            title=p.display_name,
            url=reverse("person_detail", args=[p.pk]),
            snippet=snippet(q, p.email),
            date=p.created_at,
        )
        for p in rows
    ]


@register("directory", "Owner directory", url_name="directory")
def directory(user, q):
    from community import services

    if not policy.can(user, "directory.view"):
        return []
    out = []
    for e in services.directory_entries():
        if not contains(q, e["name"], e["street_address"], e["email"], e["phone"]):
            continue
        out.append(
            Result(
                title=e["name"],
                url=reverse("directory"),
                snippet=snippet(q, e["street_address"], e["email"], e["phone"]),
                detail=e["address"],
            )
        )
    return out[:LIMIT]
