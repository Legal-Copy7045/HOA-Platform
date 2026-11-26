"""Common-area reservations (Milestone 4, optional feature "reservations")."""

import datetime

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import reservations, services
from community.models import CommonArea, Reservation, ReservationState, Role
from community.tests.factories import LotFactory, PersonFactory, days_ago, make_member
from core.models import FeatureSetting
from core.policy import SYSTEM

pytestmark = pytest.mark.django_db


def _at(days: int, hour: int) -> datetime.datetime:
    day = timezone.localdate() + datetime.timedelta(days=days)
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour)))


def _area(name="Pavilion", **rules):
    area = CommonArea.objects.create(name=name, kind="amenity")
    defaults = {"bookable": True, "needs_approval": True, "citation": "Rules §4.2"}
    defaults.update(rules)
    reservations.save_area_settings(area, actor=SYSTEM, **defaults)
    return area


@pytest.fixture
def world():
    FeatureSetting.objects.create(key="reservations", enabled=True)
    lot_a, lot_b = LotFactory(), LotFactory()
    return {
        "lot_a": lot_a,
        "lot_b": lot_b,
        "owner_a": make_member(lots=[lot_a]),
        "owner_b": make_member(lots=[lot_b]),
        "board": make_member(board=True),
        "manager": make_member(roles=[Role.MANAGER]),
        "area": _area(),
    }


def _book(world, who="owner_a", lot="lot_a", start=(3, 10), end=(3, 12), area=None, **kw):
    return reservations.request_reservation(
        area=area or world["area"],
        lot_id=world[lot].pk,
        starts_at=_at(*start),
        ends_at=_at(*end),
        actor=world[who],
        **kw,
    )


# --- Requesting ---------------------------------------------------------------------


def test_owner_requests_and_board_confirms_with_audit_and_email(world):
    res = _book(world, purpose="Lot A birthday")
    assert res.state == ReservationState.PENDING
    assert AuditEvent.objects.filter(action="reservation.requested").count() == 1
    reservations.approve(res, actor=world["board"])
    res.refresh_from_db()
    assert res.state == ReservationState.CONFIRMED and res.decided_by == world["board"]
    event = AuditEvent.objects.get(action="reservation.approved")
    assert event.metadata == {"area_id": world["area"].pk, "lot_id": world["lot_a"].pk}


def test_confirmation_email_goes_to_the_requester(world, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        res = _book(world)
        reservations.approve(res, actor=world["board"])
    subjects = [(m.subject, m.to) for m in mail.outbox]
    assert any("confirmed" in s and to == [world["owner_a"].email] for s, to in subjects)
    assert all("Lot A" not in m.body for m in mail.outbox)


def test_no_approval_needed_confirms_at_once(world):
    area = _area("Tennis court", needs_approval=False)
    res = _book(world, area=area)
    assert res.state == ReservationState.CONFIRMED


def test_owner_cannot_book_for_another_lot(world):
    with pytest.raises(reservations.ReservationError):
        _book(world, lot="lot_b")


def test_rules_are_enforced(world):
    area = _area("Clubhouse", max_hours=2, advance_days=10, monthly_limit_per_lot=1)
    with pytest.raises(reservations.ReservationError, match="at most 2 hours"):
        _book(world, area=area, start=(3, 9), end=(3, 12))
    with pytest.raises(reservations.ReservationError, match="10 days ahead"):
        _book(world, area=area, start=(20, 9), end=(20, 10))
    with pytest.raises(reservations.ReservationError, match="future"):
        _book(world, area=area, start=(-1, 9), end=(-1, 10))
    with pytest.raises(reservations.ReservationError, match="after the start"):
        _book(world, area=area, start=(3, 10), end=(3, 9))
    _book(world, area=area, start=(3, 9), end=(3, 10))
    # Same month: the limit counts per home. (Day 4 may fall in the next month.)
    first = _at(3, 9)
    same_month = first + datetime.timedelta(hours=2)
    with pytest.raises(reservations.ReservationError, match="most allowed"):
        reservations.request_reservation(
            area=area,
            lot_id=world["lot_a"].pk,
            starts_at=same_month,
            ends_at=same_month + datetime.timedelta(hours=1),
            actor=world["owner_a"],
        )


def test_area_not_bookable_is_refused(world):
    area = CommonArea.objects.create(name="Pond", kind="stormwater")
    with pytest.raises(reservations.ReservationError, match="cannot be booked"):
        _book(world, area=area)


def test_tenants_and_contacts_only_where_the_area_allows(world):
    from community.models import Account

    account = Account.objects.unscoped("t").get(lot=world["lot_a"], closed_on__isnull=True)
    tenant_user = make_member()
    tenant = tenant_user.person
    services.add_account_contact(account, person=tenant, kind="tenant", actor=SYSTEM)
    contact_user = make_member()
    services.add_account_contact(
        account, person=contact_user.person, kind="authorized", actor=SYSTEM
    )
    viewer_user = make_member()
    services.add_account_contact(account, person=viewer_user.person, kind="view_only", actor=SYSTEM)
    for user in (tenant_user, contact_user):
        with pytest.raises(reservations.ReservationError):
            reservations.request_reservation(
                area=world["area"],
                lot_id=world["lot_a"].pk,
                starts_at=_at(3, 10),
                ends_at=_at(3, 11),
                actor=user,
            )
    with pytest.raises(PermissionDenied):
        reservations.request_reservation(
            area=world["area"],
            lot_id=world["lot_a"].pk,
            starts_at=_at(3, 10),
            ends_at=_at(3, 11),
            actor=viewer_user,
        )
    open_area = _area("Clubhouse", allow_tenants=True, allow_authorized=True)
    for i, user in enumerate((tenant_user, contact_user)):
        res = reservations.request_reservation(
            area=open_area,
            lot_id=world["lot_a"].pk,
            starts_at=_at(3 + i, 10),
            ends_at=_at(3 + i, 11),
            actor=user,
        )
        assert res.lot == world["lot_a"]


def test_former_owner_cannot_book(world):
    seller = make_member(lots=[LotFactory()])
    person = seller.person
    ownership = person.ownerships.model.objects.unscoped("t").get(person=person)
    services.end_ownership(ownership, end_date=days_ago(1), actor=SYSTEM)
    with pytest.raises(PermissionDenied):
        reservations.request_reservation(
            area=world["area"],
            lot_id=ownership.lot_id,
            starts_at=_at(3, 10),
            ends_at=_at(3, 11),
            actor=seller,
        )


# --- No double booking --------------------------------------------------------------


def test_overlapping_confirmed_bookings_are_refused(world):
    first = _book(world)
    second = _book(world, who="owner_b", lot="lot_b", start=(3, 11), end=(3, 13))
    reservations.approve(first, actor=world["board"])
    with pytest.raises(reservations.ReservationError, match="already confirmed"):
        reservations.approve(second, actor=world["board"])
    with pytest.raises(reservations.ReservationError, match="already booked"):
        _book(world, who="owner_b", lot="lot_b", start=(3, 9), end=(3, 11))
    # Back to back is fine (end is exclusive).
    _book(world, who="owner_b", lot="lot_b", start=(3, 12), end=(3, 13))


def test_database_refuses_overlap_even_without_the_service(world):
    first = _book(world)
    second = _book(world, who="owner_b", lot="lot_b", start=(3, 11), end=(3, 13))
    Reservation.objects.unscoped("t").filter(pk=first.pk).update(state="confirmed")
    with pytest.raises(IntegrityError), transaction.atomic():
        Reservation.objects.unscoped("t").filter(pk=second.pk).update(state="confirmed")


# --- Decline and cancel -------------------------------------------------------------


def test_decline_and_cancel_are_audited(world):
    res = _book(world)
    reservations.decline(res, note="Pavilion closed for repairs", actor=world["manager"])
    res.refresh_from_db()
    assert res.state == "declined" and res.note == "Pavilion closed for repairs"
    other = _book(world, start=(4, 10), end=(4, 11))
    reservations.cancel(other, actor=world["owner_a"])
    event = AuditEvent.objects.get(action="reservation.cancelled")
    assert event.metadata["by"] == "owner"
    third = _book(world, start=(5, 10), end=(5, 11))
    reservations.approve(third, actor=world["board"])
    reservations.cancel(third, note="Storm", actor=world["board"])
    assert AuditEvent.objects.filter(action="reservation.cancelled").count() == 2


def test_owner_cannot_cancel_or_approve_another_lots_booking(world):
    res = _book(world, who="owner_b", lot="lot_b")
    with pytest.raises(reservations.ReservationError):
        reservations.cancel(res, actor=world["owner_a"])
    with pytest.raises(PermissionDenied):
        reservations.approve(res, actor=world["owner_a"])
    with pytest.raises(PermissionDenied):
        reservations.approve(res, actor=world["owner_b"])  # not even their own


def test_manager_cannot_change_booking_rules(world):
    with pytest.raises(PermissionDenied):
        reservations.save_area_settings(world["area"], bookable=False, actor=world["manager"])


# --- Pages --------------------------------------------------------------------------


def test_pages_are_off_unless_the_feature_is_on(client, world):
    FeatureSetting.objects.filter(key="reservations").update(enabled=False)
    client.force_login(world["owner_a"])
    assert client.get(reverse("reservations")).status_code == 404
    client.force_login(world["board"])
    assert client.get(reverse("reservation_settings")).status_code == 404


def test_owners_see_other_homes_only_as_booked(client, world):
    res = _book(world, who="owner_b", lot="lot_b", purpose="Lot B pavilion party")
    reservations.approve(res, actor=world["board"])
    pending_b = _book(world, who="owner_b", lot="lot_b", start=(6, 10), end=(6, 11))
    client.force_login(world["owner_a"])
    page = client.get(reverse("reservations")).content.decode()
    assert "Booked" in page
    assert "Lot B pavilion party" not in page
    assert world["owner_b"].person.display_name not in page
    assert reverse("reservation_detail", args=[res.pk]) not in page
    assert reverse("reservation_detail", args=[pending_b.pk]) not in page
    assert client.get(reverse("reservation_detail", args=[res.pk])).status_code == 404
    assert client.post(reverse("reservation_action", args=[res.pk, "cancel"])).status_code == 404
    # The board sees who.
    client.force_login(world["board"])
    page = client.get(reverse("reservations")).content.decode()
    assert "Lot B pavilion party" in page and world["lot_b"].label in page


def test_owner_books_and_cancels_through_the_pages(client, world):
    client.force_login(world["owner_a"])
    response = client.post(
        reverse("reservations"),
        {
            "area": world["area"].pk,
            "lot": world["lot_a"].pk,
            "starts_at": _at(3, 10).strftime("%Y-%m-%dT%H:%M"),
            "ends_at": _at(3, 12).strftime("%Y-%m-%dT%H:%M"),
            "purpose": "Picnic",
        },
    )
    res = Reservation.objects.unscoped("t").get()
    assert response["Location"] == reverse("reservation_detail", args=[res.pk])
    assert res.starts_at == _at(3, 10)
    page = client.get(reverse("reservation_detail", args=[res.pk])).content.decode()
    assert "Cancel booking" in page and "Confirm booking" not in page
    assert client.post(reverse("reservation_action", args=[res.pk, "approve"])).status_code == 403
    client.post(reverse("reservation_action", args=[res.pk, "cancel"]))
    res.refresh_from_db()
    assert res.state == "cancelled"


def test_board_approves_from_the_page_and_my_items(client, world):
    res = _book(world)
    from core.my_items import items_for

    titles = [i.title for i in items_for(world["manager"])]
    assert any(t.startswith("Booking: Pavilion") for t in titles)
    assert not any(t.startswith("Booking:") for t in [i.title for i in items_for(world["owner_a"])])
    client.force_login(world["manager"])
    client.post(reverse("reservation_action", args=[res.pk, "approve"]))
    res.refresh_from_db()
    assert res.state == "confirmed"
    assert not any(i.title.startswith("Booking:") for i in items_for(world["manager"]))


def test_my_items_hides_bookings_when_the_feature_is_off(world):
    _book(world)
    FeatureSetting.objects.filter(key="reservations").update(enabled=False)
    from core.my_items import items_for

    assert not any(i.title.startswith("Booking:") for i in items_for(world["board"]))


def test_board_saves_rules_in_dollars(client, world):
    client.force_login(world["board"])
    area = world["area"]
    prefix = f"a{area.pk}-"
    client.post(
        reverse("reservation_area_save", args=[area.pk]),
        {
            prefix + "name": area.name,
            prefix + "kind": area.kind,
            prefix + "active": "on",
            prefix + "bookable": "on",
            prefix + "max_hours": "4",
            prefix + "fee_cents": "25.00",
            prefix + "deposit_cents": "100",
            prefix + "citation": "Rules §4.3",
        },
    )
    rules = reservations.settings_for(area)
    assert (rules.fee_cents, rules.deposit_cents, rules.max_hours) == (2500, 10000, 4)
    assert rules.citation == "Rules §4.3" and not rules.needs_approval
    assert client.get(reverse("reservation_settings")).status_code == 200
    client.force_login(world["owner_a"])
    page = client.get(reverse("reservations")).content.decode()
    assert "$25.00" in page and "not paid here" in page
    assert client.get(reverse("reservation_settings")).status_code == 403


def test_area_and_its_rules_are_edited_together(client, world):
    """Streamlining: the area's own details are saved with its booking rules."""
    client.force_login(world["board"])
    area = world["area"]
    prefix = f"a{area.pk}-"
    page = client.get(reverse("reservation_settings")).content.decode()
    assert "<h1>Common area booking rules</h1>" in page
    assert f'name="{prefix}name"' in page and f'name="{prefix}citation"' in page
    response = client.post(
        reverse("reservation_area_save", args=[area.pk]),
        {
            prefix + "name": "Lakeside pavilion",
            prefix + "kind": "amenity",
            prefix + "notes": "Lights on a timer",
            prefix + "active": "on",
            prefix + "bookable": "on",
            prefix + "max_hours": "6",
            prefix + "citation": "Rules §4.4",
        },
    )
    assert response.status_code == 302
    area.refresh_from_db()
    assert (area.name, area.notes, area.active) == ("Lakeside pavilion", "Lights on a timer", True)
    rules = reservations.settings_for(area)
    assert (rules.max_hours, rules.citation) == (6, "Rules §4.4")
    actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert {"community.common_area_saved", "reservation.settings_saved"} <= actions


def test_invalid_area_form_comes_back_with_what_was_typed(client, world):
    client.force_login(world["board"])
    area = world["area"]
    prefix = f"a{area.pk}-"
    response = client.post(
        reverse("reservation_area_save", args=[area.pk]),
        {prefix + "name": "", prefix + "kind": "amenity", prefix + "max_hours": "6"},
    )
    assert response.status_code == 400
    page = response.content.decode()
    assert 'role="alert"' in page and 'value="6"' in page
    area.refresh_from_db()
    assert area.name == "Pavilion"
    # A service refusal (too long a booking) saves neither part.
    response = client.post(
        reverse("reservation_area_save", args=[area.pk]),
        {prefix + "name": "Renamed", prefix + "kind": "amenity", prefix + "max_hours": "9999"},
    )
    assert response.status_code == 400
    area.refresh_from_db()
    assert area.name == "Pavilion"


def test_retired_areas_stay_listed_and_new_ones_are_added_here(client, world):
    client.force_login(world["board"])
    response = client.post(
        reverse("common_area_save"),
        {"name": "Tennis court", "kind": "amenity", "back": "booking_rules"},
    )
    assert response["Location"] == reverse("reservation_settings")
    court = CommonArea.objects.get(name="Tennis court")
    CommonArea.objects.filter(pk=court.pk).update(active=False)
    page = client.get(reverse("reservation_settings")).content.decode()
    assert "Tennis court · not in use" in page
    # From Community settings the add form still goes back there.
    response = client.post(reverse("common_area_save"), {"name": "Pond", "kind": "amenity"})
    assert response["Location"] == reverse("community_settings")
    settings_page = client.get(reverse("community_settings")).content.decode()
    assert reverse("reservation_settings") in settings_page


def test_only_the_board_edits_areas_and_rules(client, world):
    area = world["area"]
    prefix = f"a{area.pk}-"
    data = {prefix + "name": "Hacked", prefix + "kind": "amenity"}
    for who in ("manager", "owner_a"):
        client.force_login(world[who])
        assert client.get(reverse("reservation_settings")).status_code == 403
        response = client.post(reverse("reservation_area_save", args=[area.pk]), data)
        assert response.status_code == 403
    area.refresh_from_db()
    assert area.name == "Pavilion"


def test_calendar_shows_confirmed_bookings_without_names(client, world):
    res = _book(world, who="owner_b", lot="lot_b", purpose="Lot B pavilion party")
    reservations.approve(res, actor=world["board"])
    client.force_login(world["owner_a"])
    page = client.get(reverse("calendar")).content.decode()
    assert "Common area bookings" in page and "Pavilion" in page and "Booked" in page
    assert "Lot B pavilion party" not in page
    FeatureSetting.objects.filter(key="reservations").update(enabled=False)
    assert "Common area bookings" not in client.get(reverse("calendar")).content.decode()


def test_buyer_does_not_see_the_sellers_bookings(world):
    res = _book(world)
    Reservation.objects.unscoped("t").filter(pk=res.pk).update(
        created_at=timezone.now() - datetime.timedelta(days=10)
    )
    buyer = PersonFactory()
    services.transfer_lot(
        lot=world["lot_a"], new_owners=[buyer], transfer_date=days_ago(0), actor=SYSTEM
    )
    from community.tests.factories import UserFactory

    user = UserFactory()
    buyer.user = user
    buyer.save()
    assert not Reservation.objects.for_user(user).filter(pk=res.pk).exists()
    # The seller still sees the booking they made.
    assert Reservation.objects.for_user(world["owner_a"]).filter(pk=res.pk).exists()
