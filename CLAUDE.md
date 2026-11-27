# Oak Creek Farms HOA Portal

A secure web portal for a 44-lot Pennsylvania planned community (PA Uniform Planned Community Act and the recorded Declaration). Owners see their home, documents, account and payments; the board, the manager and the Architectural Review Committee (ARC) run reviews, records, meetings, votes and the association's books. Django 5.2 LTS, PostgreSQL 16, server-rendered templates with HTMX, one Docker image (web, worker, migrate), self-hosted on a VM.

This file is the source of truth for architecture and rules. If a task conflicts with it, stop and ask rather than deviating. Update this file when a decision changes. The reasoning behind each decision is recorded in `docs/decisions.md`.

The full rules and reference live in `docs/claude/` (list below), moved there verbatim to keep this file short (D90): before changing an area, read its file. A rule there binds exactly as one here.

## Run and test

- First time: `cp .env.example .env`; run locally: `docker compose up` (site http://localhost:8000, mail http://localhost:8025).
- Before a pull request is marked ready: `python3 regression/run.py check` (`scripts/check.sh`): ruff, bandit, migrations, pip-audit, check --deploy, versions, privacy guard, the whole suite. Needs Postgres with the two roles from `docker/postgres/init-roles.sh`.
- One area: `pytest <app>/tests/`; lint: `ruff check . && ruff format --check .`
- Full regression, walkthrough, both: `/regression`, `/walkthrough`, `/full-check` (global skills; `regression/README.md`, `regression/profile.yaml`). A run fixes no product code; afterwards every finding with a clear fix goes in the next release unasked, and the owner is asked only when a fix is uncertain, weakens security, changes a legal or governance rule, or merges or removes pages.

## Rules for every change

- **Architecture**: modular monolith; apps call each other through `services.py`, never another app's models for writes; all configuration from environment variables; single-tenant; Declaration rules are data with a citation; money is integer cents.
- **Append-only**: financial, legal, decision, vote and audit records are never edited or deleted; corrections are new reversing records (model, DB trigger, runtime-role grants).
- **Authorization**: one permission map (`core/policy.py`); services take an explicit `actor` and call `core.policy.require()`; lot and person data only through scoped querysets (`core/scoping.py`), out of scope is 404; the manager never decides; no self-grants; every action performable by two people with MFA; nobody advances their own matter with board or staff powers (`core/conflicts.py`, D65). Every feature ships cross-owner access tests; new pages go in `core/tests/test_url_matrix.py`.
- **Security**: no anonymous page except the sign-in steps and the vendor onboarding form (D79); strong authentication for everyone (never password-only; an emailed code is never a second factor); uploads checked, scanned, private and streamed after an authorization check; never card or bank numbers; treat all input as hostile; third-party service keys are entered in the browser only on Keys and connections (D93: sysadmin, fresh passkey or authenticator check, tested first, encrypted with the server's `CREDENTIALS_KEK`, write-only, audited by name, the board told; Stripe and Teller need a second board member and a pinned account).
- **Privacy**: fictional data only; no secrets, tokens or real personal data in the repository, fixtures, logs, error reports or chat; logs and audit carry no personal data; service keys only through Keys and connections (`core/credentials.py`, read with `credentials.get`) or, as a fallback, the guided server scripts; server-only secrets (Django key, database, tunnel, file store, `CREDENTIALS_KEK`) stay in the server's `.env` (D93).
- **Setup wizard**: every new setting, optional feature or server set-up script appears in the Community setup wizard (`community/tests/test_setup_wizard_guard.py`).
- **Tests and suite**: every change includes tests (authorization, ownership transfer, recusal and audit are mandatory targets); a migration for every model change, never editing applied ones; a `regression/catalogue/*.yaml` entry, `regression/manual.yaml` for what only a person can check, new IDs in `regression/ids/`.
- **Releases** (D37, D90): every merge to `main` bumps `VERSION` (MINOR features, PATCH fixes) and adds its newest-first `CHANGELOG.md` section; the merged bump is the release (tag `v<VERSION>`), and the VM installs only release tags. Open pull requests as drafts; mark ready once `run.py check` passes; `ci-result` is the required check.
- Keep this file under 8 KB (a test checks); new reference material goes in `docs/claude/<topic>.md`.

## Reference (docs/claude/)

- `scope-and-stack.md`: what is built and decided (milestones, payments, votes), what is out of scope, the stack.
- `architecture.md`: the seven non-negotiable architecture rules in full.
- `authorization.md`: roles, seats and offices, accounts and contacts, owner requests, former owners, scoping, own matters, sysadmin.
- `security.md`: sign-in, MFA, recovery, invitations, notices consent, passwords, uploads, logging, anonymous access; the audit log.
- `governance.md`: ARC workflow and acknowledgements, threads, governing rules, voting and turnout, secret choices, meetings, packets, surveys, reservations, RSVPs.
- `ui-navigation.md`: addresses, merged pages, sidebar and hubs, My items, phone layout, forms, search, owner help, features, workflows, URL matrix, setup wizard.
- `finance.md`: ledger and accounts, payments and autopay, disbursements, books and close, statements and tax, finance options, special assessments, plans, reimbursements, budgets, vendor onboarding, renewals.
- `comms.md`: bounced email, push and texts, paper mail, USPS tracking.
- `operations.md`: management commands, demo data, backups and drills, host security, health and monitoring, register import, access review, server resources.
- `deployment.md`: updates and release tags, server tasks, file storage, production settings, scan queue, image pins, the POC VM, deployment targets.
- `development.md`: testing rules, commands, CI and its gates, the regression suite, releases.

## Never

- Never store or request real owner personal data, real Stripe keys, or production secrets in this environment.
- Never disable authorization, MFA, or audit logging to make a test or demo pass.
- Never edit or delete financial, decision, vote, or audit records in place.
- Never weaken a security setting without explaining the trade-off and getting approval first.
- Never expose Postgres or the object store to the public internet.
