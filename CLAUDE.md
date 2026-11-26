# Oak Creek Farms HOA Portal

A secure web portal for a 44-lot Pennsylvania planned community (governed by the PA Uniform Planned Community Act and the community's recorded Declaration). Owners log in to see their lot, documents, and (later) account and payments. Board members and the Architectural Review Committee (ARC) run reviews, records, meetings, and (later) financial reports.

This file is the source of truth for architecture and rules. If a task conflicts with it, stop and ask rather than deviating. Update this file when a decision changes. The reasoning behind each decision is recorded in `docs/decisions.md`.

## Scope

- Milestone 1 (done): accounts and MFA, lots and ownership, role-scoped access, audit log.
- Milestone 2 (directed 2026-11-25, see `docs/milestone-2-plan.md`): community definition (rules with citations, dues, common areas), owner profile and opt-in directory, calendar, announcements, board tasks, data exports, document library, meetings, resolutions and policies, records requests, voting (informal polls and formal votes), architectural review, violations, vendors and work orders, invoice approval, owner ledger with monthly dues and prepayment drawdown, budgets, collections tracking, resale certificates, reports, Stripe placeholder.
- Milestone 3 (done, `docs/milestone-3-plan.md`): accounts and contacts, owner requests and threads, committees, folders, contracts, roster, FAQ, ARC neighbor acknowledgements.
- Milestone 4 (directed 2026-11-25, `docs/milestone-4-plan.md`): backups and restore, monitoring, email preferences and digest, bulk onboarding; annual meeting and elections, budget notices, compliance calendar, written consent; accounting export, reconciliation, reminders and collections letters; reservations, PWA, SMS, access reviews, retention, accessibility.
- Decided 2026-11-25 (D21): late fees can be automatic but start manual; formal votes are binding; secret ballots are stored unlinkable; payments by card and bank transfer through Stripe hosted pages (fee part superseded by D32-payments: a surcharge on credit cards only, at most 3%; bank and debit no fee); board seats need a second approval in production; malware scanning on.
- Decided 2026-11-26 (D32-payments): open-item accounting (payments applied to specific charges); Stripe built and tested in test mode, no webhooks (pull only), restricted key without refund rights.
- Still requires a decision before use: live Stripe keys and the HOA's Stripe account.
- Out of scope: native mobile apps, multi-association tenancy, anything that stores card or bank numbers.

## Stack (decided)

- Python 3.12+ (image uses 3.13), Django 5.2 LTS, PostgreSQL 16+.
- Server-rendered templates with HTMX. No SPA, no separate JSON API unless a later milestone needs one.
- django-allauth for auth, with MFA (TOTP and WebAuthn passkeys). django-axes for login throttling. django-simple-history for model change history.
- File storage through django-storages to an S3-compatible bucket (Cloudflare R2 in production, SeaweedFS locally; MinIO no longer publishes images). Never write uploads to local disk.
- Postgres-backed background task queue, no Redis: Procrastinate.
- pytest + pytest-django, ruff, bandit, pip-audit. GitHub Actions CI.
- Docker. One image runs web, worker, and the one-shot migrate step. Docker Compose for local and homelab.
- Two Postgres roles: an owner role used only by migrations, and a runtime app role with no DDL, no TRUNCATE, and no UPDATE/DELETE on append-only tables. Web and worker never receive owner credentials.

## Architecture rules (non-negotiable)

1. Modular monolith. Apps: `accounts`, `community`, `audit`, `comms`, `documents`, `governance`, `arc`, `compliance`, `operations`, `ledger`, `payments`, `reports`. Apps call each other through service functions in `services.py`, never by importing another app's models for writes.
2. All configuration comes from environment variables (12-factor). No host-specific paths, hostnames, or secrets in code. Provide `.env.example`.
3. Portable by design: the same image must run on a Proxmox VM, a VPS, or a PaaS with only environment changes.
4. Single-tenant. No tenancy columns.
5. Anything financial, legal, or audit-related is append-only: ledger entries, ARC decisions, votes, audit events. Corrections are new reversing records, never edits or deletes. Enforce at the model layer (no update/delete paths), with a DB trigger, and by revoking UPDATE/DELETE/TRUNCATE from the runtime role (register the table with the append-only helper).
6. Rules that come from the Declaration (review deadlines, required submittals, voting thresholds, fee amounts) are data/configuration, not hard-coded, and each carries a citation field (e.g. "Declaration §X.Y").
7. Money is integer cents. Never floats.

## Authorization (critical)

- A `manager` role (management-company staff) does data entry and workflow steps; it never approves invoices, decides ARC or violations, changes rules, or votes.
- Owner access derives from Ownership. Board membership derives from an active `BoardSeat` (declarant_appointed or owner_elected, with term dates; ends only when `ended_on` is recorded). Offices (president, vice_president, treasurer, secretary) are separate `OfficeHolding` records and require an active seat. Other roles (arc_reviewer, auditor, sysadmin) are dated `RoleAssignment` records; ARC appointments record who appointed them.
- `Person` (community identity) is separate from `User` (login). Ownerships, seats, offices and roles attach to Person.
- Permissions live in one map in `core/policy.py`. For now every board member can perform every board action. Every action must remain performable by at least two people with MFA; the app warns when it is not.
- No self-grants. Granting sysadmin needs a nominator and a different approving board member; revoking needs one. Invitations grant nothing: they only link a login to an existing Person.
- Owner access derives from an active `Ownership` record (person to lot, with start and end dates). Ending an ownership removes access automatically; history stays on the lot.
- Board members are homeowners first: their ownership is recorded like anyone's (register import, or by another board member), and board rights from the seat stack on top. The menu shows a "My home" section and the board's sections (Association, Maintenance, Finance, Admin); ending a seat removes only the board side.
- Accounts (Milestone 3): an `Account` is one ownership period of a lot (numbered; dormant once the lot is sold). People not on the deed attach as `AccountContact` with a kind: authorized (sees the account, raises requests), view_only (sees, cannot act), tenant (community pages and own requests, no balance or votes). Contacts are added only by the board or manager.
- Former owners keep a read-only login that shows only their own records from their ownership period (`/my-history/`, `history.view_own`, which also lists their old requests); services refuse any change from them (`community.services._refuse_read_only`). Nothing after the sale, and no current community pages.
- Every model that belongs to a lot or person must be reachable only through a scoped manager/queryset (e.g. `Model.objects.for_user(user)`). The default manager must not return unscoped data to owner-facing views. Views must never fetch by bare primary key without scoping.
- Pattern (`core/scoping.py`): subclass `ScopedModel`, set `scope_lot_path` or `scope_person_path`, use a `ScopedQuerySet` manager. Unscoped queries raise `UnscopedQueryError`; services use `.unscoped(reason)` or `system_access(reason)`. Views use `PolicyRequiredMixin` / `ScopedObjectMixin` (`core/mixins.py`); out-of-scope objects are 404.
- Services take an explicit `actor` and call `core.policy.require()` themselves. Trusted internal code (bootstrap, seed data) passes `core.policy.SYSTEM`; views always pass `request.user`.
- Every feature ships with cross-owner access tests: owner A must get 403/404 for owner B's lot, documents, requests, and files, on every URL and download path.
- ARC recusal: a reviewer who is the applicant, or whose lot is affected, cannot view deliberations or vote on that request. Enforced in the service layer and tested.
- Sysadmin actions are audited and visible to the Board. Sysadmins get operations pages only, no owner data by virtue of the role. No Django admin in production. Break-glass management commands require `--actor` and `--reason` and are audited.

## Security rules

- Invite-only accounts tied to a lot or board role. No open signup.
- Strong authentication is mandatory for every account, owners included: a passkey, or a password or emailed sign-in code followed by TOTP or a passkey. Never password-only. Email codes never count as a second factor.
- Lost authenticators: two-board-member assisted reset plus a single-use enrollment code handed over out of band. No self-service bypass.
- A login with no authenticator may enroll one only in the browser session that accepted its invitation or redeemed a reset code, within `MFA_ENROLLMENT_WINDOW_SECONDS`; otherwise it is signed out. Re-authentication accepts a password or an authenticator, never an emailed code.
- Invitations go only to the email on the Person record and are created only by the board.
- Passwords: 12+ characters, no composition rules, common-password and breached-password (HIBP k-anonymity, fail-open) checks.
- Secure cookies, CSRF, HSTS, CSP, strict `ALLOWED_HOSTS`, `DEBUG=False` outside local dev.
- File uploads: type allowlist (by content, with Office macro/embedded-object/remote-template checks), size limits enforced before buffering, malware scan hook (rescans are new append-only records), stored in a private bucket under random keys, streamed only through the app after an authorization check (never signed URLs), with names that always end in the validated extension. Published document versions are immutable.
- No secrets, tokens, or real personal data in the repository, fixtures, logs, or error reports. Local and POC data is synthetic only.
- Log structured events without personal data. Do not log request bodies, exception messages, or URLs carrying tokens (gunicorn access logs are off).
- Proxy headers (client IP, scheme) are trusted only from `TRUSTED_PROXY_CIDRS`.
- The application never handles card or bank account numbers. (Later: Stripe hosted Checkout only.)
- Treat all user input and uploaded files as hostile.
- No anonymous access: every page needs sign-in except the sign-in steps themselves (login, invitation, reset code). Emailed links lead to signed-in pages; links that record a vote are personal and single-use, and casting needs recent re-authentication (D24).

## Audit

`audit.AuditEvent` records who did what to which object, when, and from where, for logins, MFA changes, role changes, document views and downloads, ARC actions, exports, and sysadmin access. Append-only. Provide a board-visible view of sysadmin activity.

- IP addresses live in a separate table purged after `AUDIT_IP_RETENTION_DAYS` (the purge is itself audited); the core event is permanent.
- Failed logins for unknown emails store a keyed hash (`AUDIT_HASH_KEY`), never the address. No user-agent strings. Event metadata is checked against a per-action field allowlist.

## ARC workflow (Milestone 3)

States: draft, neighbors (awaiting neighbor acknowledgements), submitted, completeness_check, in_review, decided (approved / approved_with_conditions / denied), appeal, completed. Required fields and deadlines are configuration derived from the Declaration, each with a section citation. Track the review clock (including any deemed-approval rule). Generate a decision letter PDF that cites the governing section. Keep every version of every submitted document.

## Testing and quality

- Full regression (`/regression`, `regression/README.md`): every few features or when the owner asks. Every run includes a full security audit (claims and whole codebase, plus the new code and the deployment surface). Every feature change keeps it current: tests, a `regression/catalogue/*.yaml` entry whose `match` finds them, `regression/manual.yaml` for anything only a person can check, and new IDs in `regression/ids/` committed. A regression run fixes nothing in product code; findings go to the report.

- Every change includes tests. Authorization, ownership-transfer, recusal, and audit behaviors are mandatory test targets.
- CI must pass: ruff, tests, bandit, pip-audit.
- Prefer small, reviewable commits with clear messages. Do not commit generated secrets.
- Add a migration for every model change; never edit applied migrations.

## Commands

(Fill in as they are created; keep this section accurate.)

- First time: `cp .env.example .env`
- Run locally: `docker compose up` (site http://localhost:8000, mail http://localhost:8025)
- All CI checks, stopping at the first failure: `scripts/check.sh`
- Full regression (stages, stable IDs, report): `python3 regression/run.py` then `python3 regression/report.py` (the `/regression` skill does the whole run)
- Walkthrough: /walkthrough; regression and walkthrough together: /full-check (`python3 regression/run.py --stage walkthrough` or `--with-walkthrough`, report `regression/walkthrough_report.py`)
- Tests (needs Postgres with the two roles from `docker/postgres/init-roles.sh`): `pytest` (CI runs four shards in parallel with `pytest --shard K/4`, conftest.py; a summary job keeps the single `test` check)
- Lint: `ruff check . && ruff format --check .`
- Security: `bandit -q -c pyproject.toml -r .` and `pip-audit --strict -r requirements/base.txt -r requirements/dev.txt`
- Update dependencies: edit `requirements/*.in`, then `pip-compile --generate-hashes --allow-unsafe --strip-extras -o requirements/base.txt requirements/base.in` (and the same for dev)
- Re-apply runtime DB grants: `python manage.py apply_db_grants`
- Actions fewer than two people can perform: `python manage.py check_coverage`
- First officer (once, on the server): `python manage.py bootstrap_first_officer --name ... --email ... [--also-sysadmin]`
- Seed synthetic data (needs `SEED_DEMO_ALLOWED=true`, and with production settings `DEPLOYMENT_STAGE=poc`): `python manage.py seed_demo` (also runs `seed_demo_operations`; run that alone on a community seeded before Milestone 2)
- New list/detail pages: register every URL in `core/tests/test_url_matrix.py` (PROTECTED with a lot-B target, or MEMBER_PAGES with a BOARD_ONLY_MARKERS entry)
- Workflows: declare transitions with `core.workflow.Workflow`; decisions that create records go through a service, not the generic action URL
- Break-glass (sysadmin, audited): `breakglass_end_ownership`, `breakglass_end_role`, `breakglass_lock_user`, `breakglass_enrollment_code` (only while fewer than two other board members can sign in), `rescan_files`, each with `--actor` and `--reason`
- Web/worker refuse to run as the DB owner: `python manage.py check_db_role` (run by the entrypoint)
- File store write/read/delete probe: `python manage.py check_storage`
- What the database holds (counts only, no personal data): `python manage.py data_status`
- Backups (POC VM): `sudo bash infra/poc/backup.sh [--install]`; restore drill: `sudo bash infra/poc/restore-drill.sh [--allow-unsigned] [dir]` (own scratch file store; refuses backups without the stamped keyed checksum from `infra/poc/backup_mac.py`, whose key never goes on a command line); shared shell helpers: `infra/poc/lib.sh`; file store export/import: `export_files` / `import_files`; off-site backups to a locked R2 bucket (D31): `sudo bash infra/poc/setup-offsite-backups.sh` (proves the lock with `check_offsite_lock`), drill with `restore-drill.sh --from-offsite`, code in `core/offsite.py`
- Host security (POC VM, once, idempotent): `sudo bash infra/poc/harden-host.sh` (automatic security updates, Livepatch when Ubuntu Pro is attached, auditd, nightly AIDE, Falco); alerts reach sysadmins via `python manage.py host_alert --kind ...`; System status shows the services
- Health: `python manage.py health_check [--alert]` (VM timer: `infra/poc/health-timer.sh`); routine update emails go through `comms.notify.send_update` (honours the weekly digest), legal notices never do
- Load the lot and owner register (go-live; check, then `--commit`): `python manage.py import_register register.csv` (template `docs/examples/register-template.csv`; real file never in the repo)
- On a fictional-data POC, attach yourself to a demo lot: `python manage.py demo_make_me_owner --email you@... --lot N`
- Homes are named by address (D40): show `lot.label` (address) everywhere people read, `{% home_title lot %}` for a home's heading (address title, "Lot N" only in `.lot-ref`), `lot.legal_label` ("<address> (Lot N)") only on formal documents; lists in address order (`lot.sort_key`); CSV exports start with "Address" ("Lot number" last only in finance/register exports); `core/tests/test_addresses.py` checks every page
- Merged pages (streamlining, M5): Homes and owners (`lot_list`, roster view `?view=roster`), Invitations and sign-ups (`invitations`, bulk buttons need `invite.send`), Budget and reserves (`owner_budget`, in `features.ANY_OF`), Recurring deadlines (`tasks?tab=deadlines`); `roster`, `onboarding`, `reserve_summary`, `compliance` redirect there. Board and committees (`board_and_committees`, `board_committees.view`): names and offices only. View-only contacts read their account's requests; areas are edited with their booking rules; urgent texts go from the announcement form
- Lists with an owner side and a board side use `?mine=1` (`core.scoping.own_records`); the "My home" menu links there
- Threads on requests, ARC, violations and work orders: `comms.threads` (subject fetched through its own scoping; `internal` notes staff-only and hidden from recused reviewers; emails carry a link only). Render with `{% thread_panel kind obj %}`; follow-up dates with `{% follow_up_form kind obj %}`
- New governing rule: add its `RuleSpec` in `community/rules.py` and its PA guidance in `community/pa_law.py` (a test fails otherwise); add a `fallback` only where the statute itself supplies the value
- ARC neighbor acknowledgements (code says "signoff", people see "acknowledgement"): `arc/signoffs.py` (plan neighbors locked on every request, board removes; "Request neighbor acknowledgements" moves draft to `neighbors` and emails review links; board review starts automatically when none are waiting; emailed links need sign-in (no anonymous access to any page except the sign-in steps; neighbors without a login and outside addresses use the printable form), printable form, scanned-form reading via `core/ai.py` with `GEMINI_API_KEY`; proposals need a person unless `arc.signoff_auto_confirm` is on; acknowledgements never decide a request)
- Portal search (`/search/`, `core/search.py`): each area registers a provider in `<app>/search.py` (listed in `PROVIDER_MODULES`) that checks its list page's permission and reuses that page's scoped queryset; thread hits go through `search.thread_results`; never log or audit the query; a new area needs a case in `core/tests/test_search.py`
- My items queue (`core/my_items.py`): add new "waiting on someone" work there, from scoped queries; the dashboard to-dos, the My items page (set-up warnings such as two-person coverage on top, area `Setup`) and sidebar bubbles all read it (`items_for_request`, once per request)
- Sidebar (`core/navigation.py`): menu label = page `<h1>` = the name in `docs/glossary.md` (a test checks); sections My home / Community / Association / Maintenance / Finance / Admin; rarely used pages go on a hub (`HUBS`: Settings, Community info, Voting) instead of the sidebar, and a hub with one page open to the user links straight to it; pages reached from another page mark its entry current via `SUBPAGES`; no per-area tab bars (Finance keeps one across Owner accounts, Collections, Accounting, Reports). Dates in lists: `{{ d|short_date }}` (shows the year outside the current month). Workflow steps that close or refuse something carry a `confirm=` text saying what happens next
- Forms: an invalid form re-renders with the bound form (never redirect), `{% include "partials/form_errors.html" %}` at the top and its panel open; service refusals go on the form with `form.add_error(None, ...)`. Several files in one upload: `documents.attachments.attach_many` (caller checks who may add). Disabled workflow steps with the reason: `Workflow.blocked()` passed as `blocked_steps`
- Owner-facing lot records (ARC, violations, resale) are limited to the owner's own era: use `core.scoping.owner_era_q` in `scope_for_owner`
- Optional features (board, Admin → Features): register in `core/features.py` (`FEATURES`, with every URL name); switched-off features 404 and leave the menu
- Installable app and alerts (`comms`): `/manifest.webmanifest` and `/sw.js` (signed-in; the worker caches static files only); push via `comms/push.py` + `comms/webpush.py` (RFC 8291/8292 on `cryptography`, no pywebpush; keys from `python manage.py generate_vapid_keys`; posts only to `PUSH_ALLOWED_HOSTS`); urgent texts via `comms/sms.py` (providers `none`/`twilio`, verified opt-in on My profile, Board → Urgent texts). Both are optional features ("push", "sms"), off by default
- Reserve plan (optional feature `reserves`, Finance → Reserve plan; owners: the Reserves section of Budget and reserves): math in `ledger/reserves.py` (pure, integer cents, rounding in its docstring), services in `ledger/reserve_plan.py`; "Record as adopted" writes append-only `ReserveStudySnapshot`, cited on the budget page and offered on Letters
- Bank feed (optional feature `bank_feeds`, D32-bankfeeds): `ledger/bankfeeds/` (providers `teller` (stdlib mTLS client, `api.teller.io` only) and `file` (OFX/QFX parser, CSV mapping); append-only `BankTransaction`; `matching.py` suggests, people confirm, reconcile pre-ticks; tokens Fernet-encrypted under `BANK_FEED_TOKEN_KEY`; Teller CSP only on `bank_connect`, scripts from `cdn.teller.io/connect/` only, unpinned by necessity, D34-fixinfra-2); descriptions always pass `base.clean_description` (long digit runs masked to the last four, D34-fixinfra-1); setup in `docs/runbook.md` "Bank feed"
- Open charges and online payments (D32-payments): `ledger/allocation.py` (append-only `PaymentApplication`, `open_charges`/`charge_status`/`apply`; every new ledger entry is applied by a post_save hook); Stripe in `payments/` (`checkout.py` start and verify, `sync.py` polling, payouts, `payouts_for_matching`, reconciliation; `stripe_api.py` urllib client, tests use `payments/tests/fake_stripe.py`); key on the VM with `sudo bash infra/poc/set-stripe-key.sh`; never `method="online"` by hand
- Streamlined money and ARC steps: delinquency letters only from the collection case (`collection_letter`, reprints `collection_notice_pdf`; the Home account page has "Open collection case", `lot_notice_pdf` redirects); balance to-do "Pay $X" → `pay_online?all=1` (methods first, `?choose=1` picks charges; the server always recomputes); a to-do's shortcut button is `Item.action_label`/`action_url`; staff shortcuts `core.my_items.quick_actions` / `{% quick_actions %}`; owner ARC tabs Mine / Asked of me (`arc/_owner_tabs.html`)
- Ledger money per ownership account (D33-acctfix): `ledger/accounts.py` (entries carry their `Account` from the posting date; money applies only within an account; readers take `account=ALL|CURRENT|id`; owner pages, letters and resale use `CURRENT`); refunds at Stripe: `payments.services.refund_portions` / `record_refund`; ownership date changes move entries by reversal + copy (`ledger.services.move_entries`, `account_periods_change` locks the lot), each move leaves an `EntryMoveNotice` My item for the board. The board moves a payment or credit to another account of the lot with Move to account on the Home account page (`services.reassign_entry`, `ledger.reassign`, board only, reason required, reversal + copy, audited `ledger.entry_reassigned`); a later date change leaves it there. Account period changes (D34-fixmoney) go through `ledger.services.account_periods_change`, which moves entries to the account they now belong to (reversal + copy with `moved_from`; owner pages exclude `accounts.MOVED_AWAY`); late fees follow their dues, online payments the checkout's account
- Money tools: `ledger/accounting.py` (journal CSV export, deposit batches, bank reconciliation, 1099 worksheet, courtesy reminders, collections letter sequence); letters and resale certificate wording: `ledger/letters.py`, Finance → Letters
- Voting and meetings (streamlined 2026-11-26): `governance/voting_views.py` is the one Voting list (`?status=past`, `?type=votes|elections|surveys`); `votes`/`elections`/`surveys` GET redirect there and take the create POSTs; no new informal polls (`create_vote` refuses; a poll is a one-question survey; old rows stay under Past); written consents are proposed and listed on Resolutions and policies (`consents` redirects); meetings create and cancel their calendar `Event` (`Meeting.calendar_event`, never for executive sessions); the meeting page uploads minutes (`services.upload_minutes`), links votes/elections/resolutions, and its notice step can mark a recurring deadline done
- Meeting packets (meeting page → Build packet; board or manager, `meeting.packet_build`): `governance/packets.py` (board and owner variants, PDF via `core/pdf.py` saved as a new version in the "Meeting packets" / "Meeting packets (board)" folders, append-only `MeetingPacket` + items, `.zip` streamed with only files passing the scan rule, one download at a time per person and ten per 15 minutes; owner packet never includes delinquency, violations or collections, and lists minutes only once the board marked them approved (`services.approve_minutes`, `minutes.adopt`, append-only `MinutesApproval`, D34-fixgov))
- Security review findings and fixes: `docs/security-review-2026-11.md`
- Surveys (optional feature `surveys`, on the Voting hub): `governance/surveys.py` (board drafts, opening freezes it by trigger; one response per person, append-only; anonymous answers kept apart from the `SurveyParticipation` "has responded" row, like secret ballots; My items until answered; audited CSV export)
- Common-area reservations (optional feature `reservations`): `community/reservations.py` (per-area `ReservationSettings` with citation; no double booking by exclusion constraint `reservation_no_double_booking`; owners see other homes only as "Booked")
- Event RSVPs and volunteer sign-ups (event page `calendar/<pk>/`): `comms/rsvp.py` (board/manager set cap, guests, deadline, reminder; owners/authorized/tenants answer; slots locked with `select_for_update`; owners see counts only, names and CSV `event.rsvp_names` board only; hourly `comms.tasks.event_reminders` via `send_update`)
- Quarterly access review (board only, append-only `accounts.AccessReview`, My items after 90 days): `accounts/access_review.py`; contact-details retention for people who left (rule `retention.former_owner_contact_days`, nightly task, board preview at Admin → Contact retention): `community/retention.py`
- Server resources and scanner figures (D39): the VM agent writes memory, swap and load from /proc into `UpdateStatus.host_resources` each minute (`low_since` when under 10% free); `core.health.resources()` / `_memory` (alerts after 10 minutes low) and `scanner_version()`; scan counts from `documents.services.scan_summary()`. No per-container `docker stats` (cost)
- Releases (D37): `VERSION` holds MAJOR.MINOR.PATCH (`settings.APP_RELEASE`; `APP_VERSION` stays the commit, shown as the build); every merge to `main` bumps it (MINOR for features or changes people notice, PATCH for fixes only; 1.0.0 is the go-live with real owners) and adds a `CHANGELOG.md` entry, newest first (a test checks both); System status shows the release and the newer code's number
- Updates without a terminal: System status → Check now / Install now / Install tonight (sysadmin); the VM's `infra/poc/portal-agent.sh` timer does the work (D25). Install tonight (D38) sets `UpdateStatus.state='scheduled'` and `scheduled_for` (next `UPDATE_NIGHTLY_HOUR`, default 4, site time); the agent turns it into a request when due. New `UpdateStatus` columns must be nullable or carry a `db_default`, because an update pulls the agent before migrating
- Server tasks (D36, Admin → Server tasks, `server_tasks`): sysadmins start `backup`, `restore_drill`, `restore_drill_offsite`, `offsite_lock_check`, `harden_host`, `health_check` (`core/host_tasks.py`, `system.run_task`; board sees history with `system.view_tasks`); the web app only inserts a `core.HostTask` row, the VM agent maps the kind in `infra/poc/host-tasks.sh`'s `case` to a fixed command and writes back through psql variables; `manage.py record_host_tasks` audits results. A new reusable host operation is added there, not as a script people run
- File storage on the POC: local SeaweedFS (`COMPOSE_PROFILES=local-store`) or Cloudflare R2; switch with `sudo bash infra/poc/move-files-to-r2.sh`
- POC VM: see `infra/poc/README.md` and `docs/runbook.md`; update in one step with `sudo bash infra/poc/update-portal.sh`; test outgoing mail with `sudo bash infra/poc/test-email.sh [address]`

## Never

- Never store or request real owner personal data, real Stripe keys, or production secrets in this environment.
- Never disable authorization, MFA, or audit logging to make a test or demo pass.
- Never edit or delete financial, decision, vote, or audit records in place.
- Never weaken a security setting without explaining the trade-off and getting approval first.
- Never expose Postgres or the object store to the public internet.

## Deployment targets

- Reusable host operations are started in the portal (Admin → Server tasks) and run by the VM agent from a fixed allowlist (D36). OS-level scripts are only for one-time setup (`provision-vm.sh`, the first `harden-host.sh` run) and steps that take secrets typed by a person (`setup-offsite-backups.sh`, `set-stripe-key.sh`, `move-files-to-r2.sh`): secrets never pass through the web app.
- POC: a Proxmox VM on an isolated VLAN, exposed only through a Cloudflare Tunnel (cloudflared in compose, web publishes no port), with a dev subdomain behind Cloudflare Access. No LAN access mode.
- Production: managed host or hardened VPS, behind Cloudflare, with the HOA owning all accounts.
- A full restore-and-migrate drill to a scratch environment must be done before any real data or live payments.
