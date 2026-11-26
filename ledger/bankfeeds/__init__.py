"""Bank feed (optional feature "bank_feeds", decision D32-bankfeeds).

Lines from the HOA's bank arrive two ways, both free: Teller's developer tier
(``teller.py``) or a statement file the bank lets anyone download (OFX/QFX or
CSV, ``ofx.py`` and ``csvfile.py``). They are stored append-only as
``BankTransaction`` rows and matched to the cash book (deposits, vendor
payments, returned payments) by a person (``matching.py``); confirmed matches
are pre-ticked on the monthly reconciliation. Nothing is ever posted to the
ledger from the feed.

Never stored: full account or routing numbers (last four digits only), the
access token in clear (``tokens.py``), or descriptions in audit metadata.
"""
