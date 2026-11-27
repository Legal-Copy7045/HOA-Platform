# Scope and stack

Moved from CLAUDE.md when it was cut to its essentials (D90): the same rules and reference, unchanged, read when a task touches this area. A rule here binds exactly as one in CLAUDE.md, which stays the source of truth (if a task conflicts with either, stop and ask).

A secure web portal for a 44-lot Pennsylvania planned community (governed by the PA Uniform Planned Community Act and the community's recorded Declaration). Owners log in to see their lot, documents, and (later) account and payments. Board members and the Architectural Review Committee (ARC) run reviews, records, meetings, and (later) financial reports.

## Scope

- Milestone 1 (done): accounts and MFA, lots and ownership, role-scoped access, audit log.
- Milestone 2 (directed 2026-11-25, see `docs/milestone-2-plan.md`): community definition (rules with citations, dues, common areas), owner profile and opt-in directory, calendar, announcements, board tasks, data exports, document library, meetings, resolutions and policies, records requests, voting (informal polls and formal votes), architectural review, violations, vendors and work orders, invoice approval, owner ledger with monthly dues and prepayment drawdown, budgets, collections tracking, resale certificates, reports, Stripe placeholder.
- Milestone 3 (done, `docs/milestone-3-plan.md`): accounts and contacts, owner requests and threads, committees, folders, contracts, roster, FAQ, ARC neighbor acknowledgements.
- Milestone 4 (directed 2026-11-25, `docs/milestone-4-plan.md`): backups and restore, monitoring, email preferences and digest, bulk onboarding; annual meeting and elections, budget notices, compliance calendar, written consent; accounting export, reconciliation, reminders and collections letters; reservations, PWA, SMS, access reviews, retention, accessibility.
- Decided 2026-11-25 (D21): late fees can be automatic but start manual; formal votes are binding; secret ballots are stored unlinkable; payments by card and bank transfer through Stripe hosted pages (fee part superseded by D32-payments: a surcharge on credit cards only, at most 3%; bank and debit no fee); board seats need a second approval in production; malware scanning on.
- Decided 2026-11-26 (D32-payments): open-item accounting (payments applied to specific charges); Stripe built and tested in test mode, no webhooks (pull only), restricted key without refund rights (only `rk_` keys are accepted; `sk_` refused, D87).
- Decided 2026-11-26 (D46): automatic payment of the monthly assessment through Stripe (setup-mode Checkout, off-session charges), built and tested in test mode; live use needs the HOA's Stripe account decision.
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
