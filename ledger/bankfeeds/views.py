"""Bank feed pages (Finance -> Accounting -> Bank feed). Board, manager and
auditor only; owners never see them. The optional feature "bank_feeds" gates
every URL here (core/features.py)."""

from __future__ import annotations

import secrets
import time
from functools import wraps

from allauth.account.decorators import reauthentication_required
from csp.decorators import csp_update
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core import policy
from documents.validation import UploadRejected, check_upload
from ledger.bankfeeds import csvfile, matching, ofx, services, teller, tokens
from ledger.bankfeeds.base import BankFeedError
from ledger.models import BankProvider, BankTransaction

CONNECT_SESSION_KEY = "bank_connect"
# Time from opening the (freshly re-authenticated) connect page to Teller
# handing back the enrollment.
CONNECT_WINDOW_SECONDS = 15 * 60
IMPORT_TYPES = {"ofx", "qfx", "csv"}
MAP_FIELDS = (
    ("date", "Date", True),
    ("description", "Description", True),
    ("amount", "Amount", False),
    ("debit", "Debit (money out)", False),
    ("credit", "Credit (money in)", False),
)


def _requires(action):
    """Check the policy before anything else (re-authentication included), so
    people without the right get a plain 403 rather than a password prompt."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            policy.require(request.user, action)
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def _can_view(request):
    policy.require(request.user, "bank_feed.view")


def _teller_ready() -> bool:
    return teller.configured() and tokens.configured()


@login_required
def bank_feed(request):
    _can_view(request)
    user = request.user
    accounts = list(services.connections())
    lines = matching.unmatched_lines()
    rows = matching.suggestions(lines)
    return render(
        request,
        "ledger/bank_feed.html",
        {
            "accounts": accounts,
            "file_accounts": [a for a in accounts if a.provider == BankProvider.FILE],
            "suggested": [r for r in rows if r["candidates"]],
            "unmatched": [r["line"] for r in rows if not r["candidates"]],
            "matches": matching.recent_matches(),
            "can_connect": policy.can(user, "bank_feed.connect"),
            "can_sync": policy.can(user, "bank_feed.sync"),
            "can_import": policy.can(user, "bank_feed.import"),
            "can_match": policy.can(user, "bank_feed.match"),
            "teller_ready": _teller_ready(),
            "token_key_set": tokens.configured(),
        },
    )


@login_required
@csp_update(teller.CONNECT_CSP)
@_requires("bank_feed.connect")
@reauthentication_required(enabled=lambda r: r.method == "GET")
def bank_connect(request):
    """Teller Connect. GET (after a fresh password or passkey check) shows the
    page and a one-time nonce; the enrollment comes back as a CSRF-protected
    POST carrying that nonce. The token is never put in a URL, a log or a
    page."""
    policy.require(request.user, "bank_feed.connect")
    if request.method == "POST":
        started = request.session.pop(CONNECT_SESSION_KEY, None) or {}
        nonce = request.POST.get("nonce", "")
        fresh = time.time() - float(started.get("at", 0)) < CONNECT_WINDOW_SECONDS
        if not (started.get("nonce") and secrets.compare_digest(started["nonce"], nonce) and fresh):
            messages.error(request, "That took too long or was opened elsewhere; connect again.")
            return redirect("bank_connect")
        try:
            made = services.connect_teller(
                access_token=request.POST.get("access_token", ""),
                actor=request.user,
                request=request,
            )
        except (BankFeedError, tokens.TokenKeyMissing) as exc:
            messages.error(request, str(exc))
            return redirect("bank_feed")
        messages.success(
            request, f"Connected {len(made)} account(s). The first sync runs now or tonight."
        )
        for conn in made:
            try:
                services.sync(conn, actor=request.user, request=request)
            except BankFeedError:
                messages.warning(request, f"{conn.label}: the first sync will be retried tonight.")
        return redirect("bank_feed")
    problems = []
    if not tokens.configured():
        problems.append("BANK_FEED_TOKEN_KEY is not set on the server.")
    if not teller.configured():
        problems.append(
            "Teller is not set up on the server (TELLER_APPLICATION_ID, and the certificate "
            "and key outside the sandbox)."
        )
    nonce = ""
    if not problems:
        nonce = secrets.token_urlsafe(24)
        request.session[CONNECT_SESSION_KEY] = {"nonce": nonce, "at": time.time()}
    return render(
        request,
        "ledger/bank_connect.html",
        {
            "problems": problems,
            "nonce": nonce,
            "application_id": teller.application_id(),
            "environment": teller.environment(),
            "connect_script": teller.CONNECT_SCRIPT,
        },
    )


@login_required
@require_POST
def bank_account_add(request):
    try:
        services.add_file_account(
            display_name=request.POST.get("display_name", ""),
            institution_name=request.POST.get("institution_name", ""),
            last_four_digits=request.POST.get("last_four", ""),
            actor=request.user,
            request=request,
        )
    except BankFeedError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Account added. Import a statement file into it below.")
    return redirect("bank_feed")


def _connection(request, pk):
    _can_view(request)
    return get_object_or_404(services.connections(), pk=pk)


@login_required
@require_POST
def bank_sync(request, pk):
    policy.require(request.user, "bank_feed.sync")
    conn = _connection(request, pk)
    try:
        result = services.sync(conn, actor=request.user, request=request)
    except (BankFeedError, tokens.TokenKeyMissing, tokens.TokenUnreadable) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request, f"Synced: {result['new']} new line(s), {result['seen']} already here."
        )
    return redirect("bank_feed")


@login_required
@require_POST
@_requires("bank_feed.connect")
@reauthentication_required
def bank_disconnect(request, pk):
    policy.require(request.user, "bank_feed.connect")
    conn = _connection(request, pk)
    try:
        services.disconnect(conn, actor=request.user, request=request)
    except BankFeedError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Disconnected. Its imported lines and matches are kept.")
    return redirect("bank_feed")


def _mapping(post) -> csvfile.Mapping:
    def col(name):
        value = (post.get(name) or "").strip()
        return int(value) if value.isdigit() else None

    date, description = col("col_date"), col("col_description")
    if date is None or description is None:
        raise BankFeedError("Choose the date and description columns.")
    return csvfile.Mapping(
        date=date,
        description=description,
        amount=col("col_amount"),
        debit=col("col_debit"),
        credit=col("col_credit"),
        flip=post.get("flip") == "1",
    )


def _done(request, result):
    messages.success(
        request,
        f"Imported {result['new']} new line(s); {result['seen']} were already here"
        + (
            f"; {result['skipped']} line(s) without a date or amount skipped."
            if result["skipped"]
            else "."
        ),
    )
    return redirect("bank_feed")


@login_required
@require_POST
def bank_import(request):
    policy.require(request.user, "bank_feed.import")
    conn = get_object_or_404(
        services.connections().filter(provider=BankProvider.FILE),
        pk=request.POST.get("account") or 0,
    )
    if request.POST.get("step") == "map":
        text = request.POST.get("csv_text", "")
        try:
            result = services.import_csv(
                conn, text, _mapping(request.POST), actor=request.user, request=request
            )
        except BankFeedError as exc:
            return _map_page(request, conn, text, error=str(exc))
        return _done(request, result)
    upload = request.FILES.get("file")
    if upload is None:
        messages.error(request, "Choose a statement file (OFX, QFX or CSV).")
        return redirect("bank_feed")
    try:
        kind = check_upload(upload, allowed=IMPORT_TYPES)
        limit = csvfile.MAX_BYTES if kind.ext == "csv" else ofx.MAX_BYTES
        if upload.size > limit:
            raise BankFeedError("The file is too large for a bank statement.")
        upload.seek(0)
        data = upload.read(limit + 1)
        if kind.ext == "csv":
            return _map_page(request, conn, csvfile.decode(data))
        result = services.import_ofx(conn, data, actor=request.user, request=request)
    except (UploadRejected, BankFeedError) as exc:
        messages.error(request, str(exc))
        return redirect("bank_feed")
    return _done(request, result)


def _map_page(request, conn, text, error=""):
    try:
        headers, sample = csvfile.head(text)
    except BankFeedError as exc:
        messages.error(request, str(exc))
        return redirect("bank_feed")
    chosen = csvfile.guess(headers)
    if request.POST.get("step") == "map":
        chosen = {k: _int(request.POST.get(f"col_{k}")) for k in chosen}
    return render(
        request,
        "ledger/bank_csv_map.html",
        {
            "conn": conn,
            "headers": list(enumerate(headers)),
            "sample": sample,
            "fields": [
                {"key": key, "label": label, "required": required, "selected": chosen.get(key)}
                for key, label, required in MAP_FIELDS
            ],
            "flip": request.POST.get("flip") == "1",
            "csv_text": text,
            "error": error,
        },
        status=400 if error else 200,
    )


def _int(value):
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


@login_required
@require_POST
def bank_match(request):
    policy.require(request.user, "bank_feed.match")
    try:
        if request.POST.get("action") == "undo":
            match = get_object_or_404(
                matching.active_matches(), pk=_int(request.POST.get("match")) or 0
            )
            matching.undo(match, actor=request.user, request=request)
            messages.success(request, "Match undone.")
        elif request.POST.get("action") == "confirm":
            line = get_object_or_404(BankTransaction, pk=_int(request.POST.get("line")) or 0)
            matching.confirm(
                line,
                kind=request.POST.get("kind", ""),
                object_id=_int(request.POST.get("object_id")) or 0,
                actor=request.user,
                request=request,
            )
            messages.success(
                request, "Matched. It is ticked for you on the next bank reconciliation."
            )
        else:
            raise Http404
    except BankFeedError as exc:
        messages.error(request, str(exc))
    return redirect("bank_feed")
