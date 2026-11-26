"""Bank feed services: connecting, syncing, importing files, matching.

Every function takes an explicit actor and checks the policy itself. Audit
metadata carries ids and counts only: never descriptions, amounts of single
lines, tokens or account numbers.
"""

from __future__ import annotations

import contextlib
import datetime

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from audit.services import record_event
from core import features
from core.policy import SYSTEM, require
from ledger.bankfeeds import csvfile, ofx, tokens
from ledger.bankfeeds.base import (
    AccountInfo,
    BankFeedError,
    Provider,
    clean_description,
    clean_text,
    last_four,
)
from ledger.bankfeeds.providers import FileProvider, for_connection
from ledger.bankfeeds.teller import TellerError, TellerProvider
from ledger.models import (
    BankBalance,
    BankConnection,
    BankConnectionStatus,
    BankProvider,
    BankTransaction,
    BankTransactionSource,
)

FIRST_SYNC_DAYS = 120
SYNC_OVERLAP_DAYS = 10
STALE_DAYS = 3
TOKEN_MAX = 200


def _user(actor):
    return None if actor is SYSTEM else actor


def feature_on(request=None) -> bool:
    return features.enabled("bank_feeds", request)


def connections():
    return BankConnection.objects.exclude(status=BankConnectionStatus.DISCONNECTED)


# --- Connecting ----------------------------------------------------------------------------


def connect_teller(*, access_token: str, actor, request=None, provider=None) -> list:
    """Store a Teller Connect enrollment: one connection per open bank account
    it gives access to. The token is encrypted before it is stored."""
    require(actor, "bank_feed.connect")
    token = (access_token or "").strip()
    if not token or len(token) > TOKEN_MAX or not token.isprintable() or " " in token:
        raise BankFeedError("Teller did not return a usable connection; try again.")
    encrypted = tokens.encrypt(token)  # refuses first when BANK_FEED_TOKEN_KEY is unset
    client = provider or TellerProvider(token)
    accounts = [a for a in client.list_accounts() if a.kind in ("", "depository")]
    if not accounts:
        raise BankFeedError("Teller found no checking or savings account to connect.")
    made = []
    with transaction.atomic():
        for account in accounts:
            existing = connections().filter(
                provider=BankProvider.TELLER, external_account_id=account.external_id
            )
            if existing.exists():
                conn = existing.get()
                conn.token_encrypted = encrypted
                conn.status = BankConnectionStatus.ACTIVE
                conn.last_error_status = None
                conn.save(update_fields=["token_encrypted", "status", "last_error_status"])
            else:
                conn = BankConnection.objects.create(
                    provider=BankProvider.TELLER,
                    display_name=account.name,
                    institution_name=account.institution_name,
                    account_last_four=last_four(account.last_four),
                    external_account_id=account.external_id,
                    token_encrypted=encrypted,
                    created_by=actor,
                )
            made.append(conn)
    for conn in made:
        record_event(
            "bank_feed.connected",
            actor=_user(actor),
            request=request,
            target=conn,
            metadata={"connection_id": conn.pk, "provider": "teller"},
        )
    return made


def add_file_account(
    *,
    display_name: str,
    institution_name: str = "",
    last_four_digits: str = "",
    actor,
    request=None,
) -> BankConnection:
    """A bank account kept up to date by importing statement files."""
    require(actor, "bank_feed.import")
    name = clean_text(display_name, 80)
    if not name:
        raise BankFeedError("Give the account a name, e.g. Operating checking.")
    digits = (last_four_digits or "").strip()
    if digits and not (digits.isdigit() and len(digits) == 4):
        raise BankFeedError("Enter only the last four digits of the account number.")
    conn = BankConnection.objects.create(
        provider=BankProvider.FILE,
        display_name=name,
        institution_name=clean_text(institution_name, 80),
        account_last_four=digits,
        created_by=actor,
    )
    record_event(
        "bank_feed.connected",
        actor=_user(actor),
        request=request,
        target=conn,
        metadata={"connection_id": conn.pk, "provider": "file"},
    )
    return conn


def disconnect(conn: BankConnection, *, actor, request=None, opener=None) -> BankConnection:
    """Delete the access token (and ask Teller to forget the account); the
    imported lines and matches stay."""
    require(actor, "bank_feed.connect")
    with transaction.atomic():
        conn = BankConnection.objects.select_for_update().get(pk=conn.pk)
        if conn.status == BankConnectionStatus.DISCONNECTED:
            raise BankFeedError("This account is already disconnected.")
        if conn.provider == BankProvider.TELLER and conn.token_encrypted:
            # Best effort: the token is deleted here either way.
            with contextlib.suppress(BankFeedError, tokens.TokenUnreadable, tokens.TokenKeyMissing):
                for_connection(conn, opener=opener).remove_account(conn.external_account_id)
        conn.token_encrypted = ""  # nosec B105 - clearing the stored token
        conn.status = BankConnectionStatus.DISCONNECTED
        conn.disconnected_at = timezone.now()
        conn.disconnected_by = _user(actor)
        conn.save(update_fields=["token_encrypted", "status", "disconnected_at", "disconnected_by"])
    record_event(
        "bank_feed.disconnected",
        actor=_user(actor),
        request=request,
        target=conn,
        metadata={"connection_id": conn.pk},
    )
    return conn


# --- Storing lines -------------------------------------------------------------------------


def store_lines(conn, lines, *, source, actor) -> tuple[int, int]:
    """Insert lines not seen before on this connection. (new, already there)."""
    require(actor, "bank_feed.import")
    by_id = {}
    for line in lines:
        by_id.setdefault(line.bank_id, line)
    existing = set(
        BankTransaction.objects.filter(connection=conn, bank_id__in=list(by_id)).values_list(
            "bank_id", flat=True
        )
    )
    fresh = [
        BankTransaction(
            connection=conn,
            bank_id=line.bank_id,
            posted_on=line.posted_on,
            amount_cents=line.amount_cents,
            description=clean_description(line.description),
            kind=clean_text(line.kind, 40),
            source=source,
            imported_by=_user(actor),
        )
        for bank_id, line in by_id.items()
        if bank_id not in existing
    ]
    BankTransaction.objects.bulk_create(fresh, ignore_conflicts=True)
    return len(fresh), len(existing)


def _store_balance(conn, balance, source) -> None:
    if balance is not None:
        BankBalance.objects.create(
            connection=conn,
            as_of=balance.as_of,
            balance_cents=balance.balance_cents,
            source=source,
        )


# --- Syncing (Teller) ---------------------------------------------------------------------


def sync(conn: BankConnection, *, actor, request=None, provider: Provider | None = None) -> dict:
    require(actor, "bank_feed.sync")
    if conn.provider != BankProvider.TELLER:
        raise BankFeedError("This account is updated by importing statement files.")
    if conn.status == BankConnectionStatus.DISCONNECTED:
        raise BankFeedError("This account is disconnected.")
    today = timezone.localdate()
    if conn.last_synced_at:
        since = timezone.localdate(conn.last_synced_at) - datetime.timedelta(SYNC_OVERLAP_DAYS)
    else:
        since = today - datetime.timedelta(FIRST_SYNC_DAYS)
    try:
        client = provider or for_connection(conn)
        lines = client.transactions(conn.external_account_id, since)
        balance = client.balance(conn.external_account_id)
    except TellerError as exc:
        conn.last_error_status = exc.status
        if exc.needs_reauth:
            conn.status = BankConnectionStatus.NEEDS_REAUTH
        conn.save(update_fields=["last_error_status", "status"])
        record_event(
            "bank_feed.sync_failed",
            actor=_user(actor),
            request=request,
            target=conn,
            outcome="failure",
            metadata={"connection_id": conn.pk, "status": exc.status},
        )
        raise
    with transaction.atomic():
        new, seen = store_lines(conn, lines, source=BankTransactionSource.TELLER, actor=actor)
        _store_balance(conn, balance, BankTransactionSource.TELLER)
        conn.last_synced_at = timezone.now()
        conn.last_error_status = None
        conn.status = BankConnectionStatus.ACTIVE
        conn.save(update_fields=["last_synced_at", "last_error_status", "status"])
    record_event(
        "bank_feed.synced",
        actor=_user(actor),
        request=request,
        target=conn,
        metadata={"connection_id": conn.pk, "new": new, "seen": seen},
    )
    return {"new": new, "seen": seen}


def sync_all() -> int:
    """The daily task: every connected Teller account (feature on only)."""
    if not feature_on():
        return 0
    done = 0
    for conn in connections().filter(provider=BankProvider.TELLER):
        try:
            sync(conn, actor=SYSTEM)
        except (BankFeedError, tokens.TokenKeyMissing, tokens.TokenUnreadable):
            continue
        done += 1
    return done


# --- Importing statement files -----------------------------------------------------------


def _file_target(conn: BankConnection) -> None:
    if conn.status == BankConnectionStatus.DISCONNECTED:
        raise BankFeedError("This account is disconnected.")


def _pick_account(accounts: list[AccountInfo], conn: BankConnection) -> AccountInfo:
    if len(accounts) == 1:
        return accounts[0]
    same = [a for a in accounts if conn.account_last_four and a.last_four == conn.account_last_four]
    if len(same) == 1:
        return same[0]
    raise BankFeedError(
        "The file holds several accounts; add the last four digits to this account so the "
        "right one is imported."
    )


def import_ofx(conn: BankConnection, data: bytes, *, actor, request=None) -> dict:
    require(actor, "bank_feed.import")
    _file_target(conn)
    statement = ofx.parse(data)
    provider = FileProvider.from_ofx(statement)
    account = _pick_account(provider.list_accounts(), conn)
    if conn.account_last_four and account.last_four and account.last_four != conn.account_last_four:
        raise BankFeedError(
            f"This file is for the account ending {account.last_four}, not "
            f"{conn.account_last_four}."
        )
    lines = provider.transactions(account.external_id, datetime.date(1900, 1, 1))
    with transaction.atomic():
        new, seen = store_lines(conn, lines, source=BankTransactionSource.OFX, actor=actor)
        _store_balance(conn, provider.balance(account.external_id), BankTransactionSource.OFX)
        if not conn.account_last_four and account.last_four:
            conn.account_last_four = account.last_four
            conn.save(update_fields=["account_last_four"])
    return _imported(conn, "ofx", new, seen, statement.skipped, actor, request)


def import_csv(conn: BankConnection, text: str, mapping, *, actor, request=None) -> dict:
    require(actor, "bank_feed.import")
    _file_target(conn)
    lines, skipped = csvfile.parse(text, mapping)
    with transaction.atomic():
        new, seen = store_lines(conn, lines, source=BankTransactionSource.CSV, actor=actor)
    return _imported(conn, "csv", new, seen, skipped, actor, request)


def _imported(conn, source, new, seen, skipped, actor, request) -> dict:
    record_event(
        "bank_feed.imported",
        actor=_user(actor),
        request=request,
        target=conn,
        metadata={
            "connection_id": conn.pk,
            "source": source,
            "new": new,
            "seen": seen,
            "skipped": skipped,
        },
    )
    return {"new": new, "seen": seen, "skipped": skipped}


# --- Statement balance from the feed -------------------------------------------------------


def feed_balance(conn: BankConnection, on: datetime.date) -> int | None:
    """The account's balance at the end of ``on``, worked out from the nearest
    balance the bank reported and the lines between; None when unknown."""
    after = (
        BankBalance.objects.filter(connection=conn, as_of__gte=on).order_by("as_of", "-id").first()
    )
    if after is not None:
        moved = (
            BankTransaction.objects.filter(
                connection=conn, posted_on__gt=on, posted_on__lte=after.as_of
            ).aggregate(t=Sum("amount_cents"))["t"]
            or 0
        )
        return after.balance_cents - moved
    before = BankBalance.objects.filter(connection=conn, as_of__lt=on).order_by("-as_of", "-id")
    before = before.first()
    synced = conn.last_synced_at and timezone.localdate(conn.last_synced_at) >= on
    if before is None or not synced:
        return None
    moved = (
        BankTransaction.objects.filter(
            connection=conn, posted_on__gt=before.as_of, posted_on__lte=on
        ).aggregate(t=Sum("amount_cents"))["t"]
        or 0
    )
    return before.balance_cents + moved


def needs_attention(conn: BankConnection) -> bool:
    """For the health check: a Teller account not synced lately or asking to
    be reconnected."""
    if conn.status == BankConnectionStatus.NEEDS_REAUTH:
        return True
    if conn.last_synced_at is None:
        return timezone.now() - conn.created_at > datetime.timedelta(days=STALE_DAYS)
    return timezone.now() - conn.last_synced_at > datetime.timedelta(days=STALE_DAYS)


# --- For the online-payments reconciliation (payments.sync) --------------------------------


def deposits_between(
    start: datetime.date, end: datetime.date
) -> list[tuple[int, datetime.date, int]]:
    """Money in (id, posted date, cents) on every connected account between
    ``start`` and ``end``, for matching Stripe payouts to the bank."""
    from ledger.models import BankTransaction

    return list(
        BankTransaction.objects.filter(posted_on__gte=start, posted_on__lte=end, amount_cents__gt=0)
        .order_by("posted_on", "id")
        .values_list("id", "posted_on", "amount_cents")
    )


def lines_through() -> datetime.date | None:
    """The newest posted date the bank feed holds, so callers only compare
    dates the bank has actually reported."""
    from django.db.models import Max

    from ledger.models import BankTransaction

    return BankTransaction.objects.aggregate(m=Max("posted_on"))["m"]
