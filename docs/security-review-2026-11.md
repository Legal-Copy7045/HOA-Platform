# Security review, November 2026

> **Scope of this document.** Sections 1 to 6 are the review of the portal
> after Milestone 2 and the UI sprint (commit `c850171`); they do not cover
> Milestones 3 and 4 or the later streamlining, payments, bank feed and
> walkthrough work. A fresh full review of the current codebase is
> scheduled: every run of the project's full regression (`/regression`,
> `regression/README.md`) includes a full security audit (the claims below,
> the whole codebase, the new code and the deployment surface), and its
> findings go to that run's report. Section 7 is a later finding (D56);
> section 8 lists the findings of the external review of 2026-11-26 and
> their status; section 9 is the invitation and enrollment review (D59).

Scope: the whole portal after Milestone 2 and the UI sprint (commit `c850171`).
Five independent reviews ran in parallel, each proving findings with scripts
where possible:

1. authorization and object scoping;
2. sign-in, MFA, sessions, invitations and account recovery;
3. file handling, generated output (PDF, CSV, email) and injection;
4. data integrity, money, append-only records, concurrency and the audit log;
5. configuration, deployment and supply chain.

Every finding was then checked against the code, fixed with a regression
test, and the fixes were reviewed again by a separate reviewer.

**Result:** 4 high, 14 medium and 23 low findings. Every high and medium
finding is fixed. Two items need a board decision (section 3). A few residual
risks are accepted and listed (section 4).

Severity scale: **High** = someone could take over an account or decide
something they must not. **Medium** = data shown to the wrong person, records
that could be quietly wrong, or an attacker could block the site. **Low** =
defence in depth, or needs unusual access.

## 1. High

| ID | Finding | Fix |
|---|---|---|
| A1 / B1 | A sysadmin (or any board member) could send a person's invitation to **any email address**. Accepting it linked the attacker's login to that person, so a newly elected board member's seat, or an owner's lot, could be taken over before they signed in. | Invitations go **only** to the email on the person's record. Only the board can create them; a sysadmin can only re-send or revoke. |
| A2 | An ARC reviewer who owned an affected neighboring lot could **clear the affected-lots list**, stop being recused, and decide the request. | A recused person can never change the list. Lots can only be **added** once review starts. The list is frozen after a decision, and every change is audited by lot number. |
| E1 | Behind the Cloudflare tunnel every visitor appeared to come from one address, so allauth's sign-in rate limits were **site-wide**: one person sending about 30 requests a minute could lock **everyone** out of signing in. | Rate limits use the real client address established by the trusted-proxy middleware. |
| B2 | An owner who accepted an invitation but never added a passkey or app could be **taken over by anyone who could read their email**: reset the password, sign in (no second factor existed yet), enroll their own authenticator. The same applied after an assisted reset. | A login with no authenticator can add one **only in the browser session that accepted the invitation or redeemed the reset code, within an hour**. Otherwise it is signed out and sent to a page asking for a new invitation or reset. |

B2 was rated medium by its reviewer; it is listed here because it is an
account takeover.

## 2. Medium and low (all fixed)

### Sign-in and accounts
- **B3:** an invitation link opened before a "re-send" kept working. Re-sending now kills the old link at once, and acceptance checks that its link is still current.
- **B4:** the board is now emailed when an assisted-reset code locks after wrong guesses.
- **B5:** production now refuses to start without a trusted proxy configured. Otherwise every visitor shares one address, the problem behind E1.
- **B6:** re-authentication before sensitive changes (adding or removing an authenticator, recovery codes) no longer accepts an **emailed code**. It needs a password or an authenticator, so email is never a factor.
- **E7:** `print_invitation_link` only works before the first login exists, to recover a lost bootstrap link. Command-line events now appear on the board's sysadmin activity page.

### Privacy
- **A4:**
  - After a sale, the buyer could read the seller's ARC requests, closed violation notices, written responses and resale paperwork. Owners now see lot records from their own time owning it. Still-open notices are an exception, because the buyer inherits the condition, but the seller's replies stay hidden.
  - Owners saw board members' and managers' email addresses in workflow history. They now see "the board", "the architectural committee" and so on.
- **A7:** owners no longer see a violation before its notice is sent.
- **D1 / E2:** error and CSRF logs contained full URLs, so invitation and password-reset tokens could land in logs. Paths, query strings and the request object are now never logged.

### Decisions and money
- **A5:** a board member who applied for ARC approval could mark their own request complete. Completion and late attachments now respect recusal. A denied appeal can no longer be "completed".
- **A6:** one board member could change the $500 dual-approval threshold, then approve a large invoice alone. The approvals an invoice needs are now fixed when it is submitted.
- **D2:** a bounced check, or a reversed mistaken charge, made old debt look current. That could drop a lot off the collections list. Reversals now cancel the entry they reverse when aging.
- **D3:** a ballot could be accepted after a vote closed, from a page opened before. Casting a ballot or closing the vote now locks it.
- **D4:**
  - A **tie** was reported as passing.
  - Thresholds were compared on rounded percentages, so 66.67% counted as 67%.
  - Ties now pass nothing, and thresholds use exact counts.
- **D5:** an issued **resale certificate** was rebuilt from today's data on every download, so a reprint could differ from what the buyer received. The issued PDF is now stored permanently and served unchanged. Disclosure edits are audited.
- **D6:**
  - The database now prevents changes to a vote's rules after it opens, and to an invoice's amount, vendor or number after submission.
  - Each approval records the amount approved.
  - Adopted meeting minutes can no longer be silently swapped; corrections are new versions of the document.
- **D8:** the invoice payment service accepted negative amounts. It now refuses them.
- **D9:** a rule value like "²" could be saved and then break dues posting. Values must be plain whole numbers.
- **D10:** one open collection case per lot, and one superseding resolution per resolution, are now enforced by the database. Reviewer positions lock the request.
- **D11:** one conflicting request no longer stops the daily deemed-approval run for every other request.
- **D7:** two internal code paths could attempt edits to append-only records. They were already blocked by the database; the code now refuses as well.

### Files and output
- **C1:**
  - An owner could upload `plan______.hta…x.pdf`. Shortening the name made it download as an `.hta` file, which Windows runs as a program when a reviewer opens it.
  - Stored and downloaded names now always end in the extension that was checked.
- **C2:**
  - Attachments ignored "only download clean files". Scan results also could not be updated after a later detection.
  - Attachments now follow the same rule as documents.
  - New `rescan_files` command: every file can be rescanned, and a file found infected later is blocked immediately.
  - Deployment checks and System status warn when no scanner is configured.
- **C3:** Word and Excel files that load remote templates or contain embedded objects or ActiveX controls are refused. Text files are checked in full.
- **C4:**
  - Oversized uploads are refused before they are read into memory.
  - Each ARC request, violation or invoice can hold at most 40 files.
- **C5:** CSV exports now neutralise formulas that start after a space or with full-width signs.
- **C6:** a line break in an announcement or meeting title made its emails fail part-way through a send. Titles are now single-line.

### Deployment
- **E3:**
  - cloudflared now sits on its own network with only the web container. A mis-routed tunnel hostname can no longer reach the database or file store.
  - **E9:** cloudflared's trusted fixed address can no longer be handed to another container.
- **E4:**
  - Demo data can only be loaded on a deployment marked `DEPLOYMENT_STAGE=poc`, and only into a demo community. Before, it could write fake logins, rules and permanent ledger entries into a real one.
  - The image defaults to production settings.
- **E5:** no `.env` variant (for example `.env.bak`) can be copied into the image or committed.
- **E6:** web and worker refuse to start if connected as the database owner, so the append-only protection can't be silently lost on a new host.
- **E8:** CI actions are pinned to exact commits.
- **E10:**
  - Permissions-Policy header added.
  - Application code is root-owned in the image, so the running app cannot change it.

## 3. Needs a board decision

1. **Recording a board seat takes one board member (A3).**
   - **Risk:** one member could create a person, give them a seat, invite their own second email address, and then act as the "second board member" on invoice approvals, sign-in resets and sysadmin nominations.
   - **Mitigation now:** the whole board is emailed whenever a seat or role is recorded.
   - **Full fix:** a second board member approves every new seat, like sysadmin nominations. The first seats would be exempt while fewer than two board members exist.
   - **Recommendation:** adopt it before real use.
2. **Secret ballots are secret in the portal, not in the database (D12).**
   - The ballot table stores who voted and how, which is what lets the portal enforce one ballot per lot. Board pages, reports and the audit log never show choices on a secret vote, but someone with direct database access could see them.
   - Truly anonymous ballots need a different design (separating "has voted" from "the choice"). Counsel should say whether that matters for formal votes.
   - **Update:** separated by D21 (2026-11-25); the remaining database-level link is fixed by D56 (2026-11-26), see section 7.

## 4. Accepted residual risks

- **File store permissions:** the app's file-store key can still create its bucket ("Admin" on that one bucket). The store's internal ports are reachable only from the app's own containers.
- **Image updates:** cloudflared follows its latest release (D13), and base images are pinned by version tag, not digest. Dependabot proposes updates.
- **POC installer:** the Debian image download is checked against a checksum file from the same server, without a signature check.
- **Break-glass attribution:** `--actor` is typed by whoever has shell access, so it relies on their honesty (D9).
- **HSTS:** the POC uses a short HSTS time without subdomains. Turn both up before production (runbook checklist).
- **Malware scanning** is still off on the POC. To turn it on: run a ClamAV container, set `DOCUMENT_SCANNER=clamd` and `DOCUMENT_REQUIRE_CLEAN_SCAN=true`, then run `rescan_files`.

## 5. Second iteration: re-audit of the fixes

A separate reviewer then tried to break every fix, end to end. It found:
- **Two lockouts the new sign-in rules caused.** Both fixed:
  - Removing your only passkey or app now isn't allowed: add the new one first.
  - New break-glass `breakglass_enrollment_code` (sysadmin, audited) helps a first officer who missed the setup window. It works only while fewer than two other board members can sign in.
- **Gaps in the first round of fixes**, all fixed:
  - An owner's history disappeared when their ownership was re-recorded (adding a spouse, a deed into a trust). Visibility now counts from the start of the *unbroken* ownership.
  - A violation dismissed before any notice became visible to the owner. Owners now see only violations a notice was sent for.
  - A seller kept their ARC request after selling while owning another lot. The applicant keeps it only while they still own the lot.
  - Office checks missed single-quoted attributes, external images on file shares, DDE fields and Excel external links. Every outside link except a plain hyperlink is now refused.
- **Low items**, all fixed:
  - The attachment cap could race; it is now locked.
  - An owner could create unlimited draft ARC requests; the cap is 5 open drafts.
  - Invoices already pending at deploy now get their fixed approval count (data migration).
  - Open invitations to an address other than the person's own are revoked (data migration). Re-send and accept also check the address.
- **Accepted:** Django's internal "base manager" is still unrestricted on the scoped append-only tables (such as the ledger). The database trigger and grants refuse any change there, as the reviewer confirmed.

It confirmed end to end that these still work: first-officer bootstrap, assisted reset, production start-up with the POC's settings, and the new network layout.

## 6. Verification

- Every fixed finding has a regression test, mostly in `core/tests/test_security_review.py` and in each app's tests, marked "Security review 2026-11". There are now over 680 automated tests.
- ruff, bandit, the migration check and pip-audit (no known vulnerabilities) are clean.
- The new regression suite (`regression/`, `/regression`) runs all of this plus a browser crawl of every page per role. Its first run found two navigation bugs (fixed).

## 7. Later finding: secret choices linkable by transaction id (fixed 2026-11-26, D56)

- **Finding (external review, Medium):** a secret ballot's "this home voted" row and its choice row were written in the same database transaction, so PostgreSQL's hidden `xmin` column (the inserting transaction id) was the same on both. Anyone who can read both tables (the runtime role can) could join them on vote and `xmin` and see how each home voted. Insertion order (physical position, timestamps) leaked the same way. Elections and anonymous surveys had the same layout. D23 had disclosed this as a known limitation.
- **Fix:** casting now records the participation and puts the choice in a mixing pool (`governance_pendingsecretchoice`) that carries no person, lot, ballot, participation or time. A separate transaction (a worker task after the cast commits, an hourly sweep, and the close itself) writes pooled choices to the permanent append-only tables in shuffled order with fresh random ids. While voting is open it writes only once twice `SECRET_BALLOT_MIX_MIN` (default 5) are waiting and keeps that many behind, so every batch, including the last one at close, mixes at least five ballots whenever five or more were cast. Closing writes everything before results are computed. The runtime role may insert into, read and delete from the pool, never update or truncate it; the permanent tables stay append-only (model, trigger, grants).
- **Tests:** `governance/tests/test_secret_mixing_transactions.py` casts in real committed transactions and checks that no permanent choice row shares `xmin` with any participation row (votes, elections, surveys); `governance/tests/test_secret_mixing.py` covers shuffled order, flush thresholds, close and results, double voting, eligibility, grants and that nothing logged or audited carries the choice.
- **Residual risk (accepted):** while a choice waits in the pool, someone with live database access, a backup taken in that window, or the write-ahead log could still link it. After the flush and VACUUM the permanent tables cannot be linked. Backups keep the pool so a restore never loses a ballot (see D56). Votes cast before this release keep the old linkable layout (demo data only); the board should not run real secret votes on an older version.
- **Follow-up (D68, 2026-11-27):** the window is closed for everyone without the server's key: pooled choices are sealed (AES-GCM, key from `DJANGO_SECRET_KEY`, padded to one size) before the casting transaction writes them, so its shared `xmin`, WAL record and dump position reach only ciphertext; kept-behind choices are re-sealed at each flush. Root on the server (key plus data) remains able to link pooled choices; see D68 for the residual risks. Tests: `governance/tests/test_secret_ballot_unlinkable.py`.

## 8. External review 2026-11-26

An external review of the current code and deployment. Decision D57 records
the reasoning; each item has tests.

| # | Finding | Status |
|---|---|---|
| X1 | Production could start with malware scanning off, downloads of unscanned files allowed, all mail redirected to one inbox, or a one-hour HSTS. | **Fixed.** `core/settings/production.py` refuses, on the POC and in production, any scanner other than ClamAV and downloads without a clean scan; in production also a mail redirect and HSTS under a year (default now a year there). The tunnel overlay sets the scanner itself. |
| X2 | The Backups health check passed with no off-site copy and never asked for a restore drill. | **Fixed.** In production: no off-site copy, one older than 36 h, or no passed restore drill in 90 days fails it. The drill now records itself. POC/local say "Not production-ready". |
| X3 | Files waiting for a malware scan were a bare count; nobody could tell what they were or whether they were stuck. Root cause: a scan that failed at upload (ClamAV not answering yet) was never retried. | **Fixed.** Retry after upload, a 10-minute sweep while ClamAV answers, a list on System status in operations terms only (no names or owners), "Scan waiting files now" (audited, counts only) and a health check after 30 minutes. |
| X4 | django-allauth 65.22.1: rate limits could be bypassed with IPv4-mapped IPv6 addresses (the app relies on allauth's limits for sign-in, codes and resets). | **Fixed.** 65.23.0 (`requirements/base.in` floor, hashes recompiled). The OIDC CIMD SSRF in the same release does not apply (no OIDC provider). |
| X5 | No secret scanning, container image scanning or static analysis in CI. | **Fixed.** Gitleaks (whole history weekly and on demand, new commits on every PR), Trivy on the built image (fails on fixable HIGH/CRITICAL), CodeQL for Python and JavaScript. Separate checks; the `test` check is unchanged. |
| X6 | The regression page crawl ran only by hand. | **Fixed.** `pages` job in CI (about 3 minutes locally, 15-minute limit). |
| X7 | Infrastructure images floated (`cloudflared:latest` pulled daily on the VM, `postgres:16-alpine`, `clamav:1.4`). | **Fixed, with the owner's condition**: pinned to version and digest only together with Dependabot's docker-compose updates, so every bump is a tested pull request; the daily cloudflared pull is removed. Left on tags: the Dockerfile base (weekly refresh) and the CI Postgres service image (Dependabot cannot update it). |
| X8 | Documentation drift: README described Milestone 1, late-fee text said fees were off, this document's scope was unclear, and there was no go-live checklist for the HOA's own third-party accounts. | **Fixed.** README, late-fee text (plan, summary, runbook, settings comment), this header, and the runbook's go-live checklist (Stripe, Teller, Twilio, Cloudflare, Gemini data-use terms, production settings, off-site backups and a restore drill, secret-ballot fix deployed before real secret votes). |
| X9 | A fresh full review of the current codebase. | **Scheduled**: the next full regression run (`/regression`) includes it. |

## 9. Invitation and enrollment review (2026-11-26)

Goal: every login ends up with a strong second factor (passkey or authenticator app), and no path lets a login reach portal data without one. Traced end to end from invitation creation to the first dashboard, at commit `9918ea9`. Decision D59 records the fixes. Line numbers are those at `9918ea9` unless marked "now".

### Findings

| ID | Severity | Where | Scenario | Status |
|----|----------|-------|----------|--------|
| IE-1 | Medium | `accounts/services.py:59` (`create_invitation`), `templates/accounts/setup_expired.html:8` | An owner accepts the invitation and closes the browser (or the hour runs out) before adding a passkey or app. From then on every sign-in is signed out. The page told them to "ask the board to send you a new invitation", but `create_invitation` refused anyone with a login ("This person already has a login"), so the board could not. The only way back was the two-board-member reset, which nothing pointed the board to. The owner was stuck, with wrong instructions. | Fixed: the board can send a **new setup link** to a login that has never had a passkey or app (`setup_again_refusal`, now `accounts/services.py:38`). Anyone who ever had one, went through a reset, has a switched-off login, or whose login email differs from the record still needs the two-person reset. The link goes only to the Person's email and is created only by the board (`invite.send`). It is single use, and it is checked again at acceptance (now `accounts/services.py:274`). Accepting sets a new password and ends every other session of the login (`session_generation`). The expired page is now "Your setup link expired" and gives both next steps. |
| IE-2 | Medium | `accounts/services.py:262` (`onboarding_rows`) | Sign-ups showed such a login as "Signed up", so the board could not see who was stuck, and nothing reminded anyone. | Fixed: new status "Signed in, no passkey or app yet" with its own filter, a note on the Signed up tile, and a "Send a new setup link" button (or the reason a reset is needed) on the row and on the person's page. A board My items entry appears for a login with no passkey or app 7 days after it last accepted a link, unless a setup link or reset is open (`stuck_logins`). |
| IE-3 | Low | `accounts/middleware.py:82` | A login with no authenticator and no setup window is signed out with no audit record of why. Only `auth.login` and then `auth.logout` were recorded. | Fixed: `auth.setup_window_closed` (reason `expired` or `none`) is recorded before the sign-out. |
| IE-4 | Low | `accounts/views.py:29`, `:43` | After the Person's email was changed, an older link still opened the password page. It was refused only when the password was saved. | Fixed: the landing page and the password page both check the address on record (`services.matches_record`). |
| IE-5 | Low (hardening) | `accounts/middleware.py:17-50` | The pages open to a login without an authenticator are a name list plus every URL name starting with `mfa_`. Nothing stopped a later page from joining that list by name, or from being added to it without review. | Fixed with tests (`accounts/tests/test_enrollment_review.py`): (1) the open list must equal the reviewed list; (2) every `mfa_` page must be allauth's or our subclass of it; (3) every named URL in the project (paths built from each pattern) redirects a password-only login inside its window to setup; (4) every named URL signs out a login with no window. |
| IE-6 | Info | `accounts/views.py:24` | The invitation link and the password page have no rate limit of their own. | Accepted. The token is 256 bits (`secrets.token_urlsafe(32)`), only its SHA-256 is stored, and the password page needs the token's hash in the session. There is nothing to guess. |
| IE-7 | Info | `accounts/setup.py` | The setup pages don't say how much of the hour is left. | Not fixed (UX only). The expired page explains what happened and what to do. |

### Confirmed, no change needed

- **Creation:** only the board (`invite.send`) creates invitations, always to the email on the Person record. Bulk invite, reminders and approval-sent invitations (D41) all go through `create_invitation`. A sysadmin can re-send or revoke but cannot create one. An invitation grants nothing: it only links a login to the existing Person.
- **The link:** single use. The hash is cleared on accept, re-send and revoke. It expires after `INVITE_TTL_HOURS`. Opening it swaps the token for a session marker and cycles the session key, so it leaves the URL bar. A second browser, a revoked link, a re-sent link and an accepted link all get "not valid". A signed-in visitor is refused. An already-linked Person cannot be invited again, except for the IE-1 setup link.
- **Acceptance:** the password validators apply (12+ characters, common and breached checks). `login()` cycles the session key again. The setup window is granted only in that session.
- **The setup window (`RequireMFAMiddleware`):** checked on every request, before every other app middleware. With no passkey or app, only logout, re-authentication (password or authenticator, never an emailed code), login, change password, health, the reset-code page and allauth's own MFA pages open. Static files are served by WhiteNoise. The manifest, `sw.js`, search, My items, the dashboard, the notice step, `account_setup_done`, every JSON and HTMX endpoint, every download, and unknown paths (404) all redirect to setup, or sign out once the window has passed. Healthz returns only up/down.
- **Emailed codes:** an emailed sign-in code is only a first factor. A login with no authenticator that signs in by code (or by password) is signed out at its next request (tested). Re-authentication by code is refused (`ACCOUNT_REAUTHENTICATION_METHODS`; allauth raises PermissionDenied on the code views).
- **Setup steps:** passkey first, with the authenticator app offered on the step page and on the passkey page. The recovery codes are shown, with Print and "I've saved them". The notice step (D44) comes only after a passkey or app exists. Neither the last passkey nor the last app can be removed (MFA adapter plus `accounts/mfa_views.py`), and adding or removing one needs a recent password or authenticator.
- **Accounts made by commands:** the first officer (`bootstrap_first_officer`) gets an invitation link and the same window. If they miss it, `breakglass_enrollment_code` covers them while fewer than two other board members can sign in. Seeded demo logins have no password, and if they sign in by code they are signed out like anyone else. There is no keep-logins flow at this commit. Django admin is refused in production (`core/settings/production.py:48`).
- **Rate limits:** axes (5 failures per email and IP), allauth's login-by-code limits (3 tries per code, plus its request limits), and 5 tries per reset code, after which the board is told.
- **Audit:** invitation created, sent, re-sent, revoked and accepted (now with `setup_again`); authenticator added and removed; recovery codes; reset steps; sign-out without a window (new).
- **Logging:** paths are masked in Django's request loggers, gunicorn access logs are off, error signatures never hold the path, the referrer policy is same-origin, and the landing page (`/`) shows a signed-out visitor only a static welcome (the site name and a Sign in button, `templates/home.html`): no portal data, nothing about any account (corrected 2026-11-27, SEC-004; it renders, it does not redirect).

### Note for the concurrent recovery work

A forgotten-password or self-service recovery flow must not open the setup window, and must not count an emailed code or link as a factor. A login with no passkey or app that resets its password and signs in is still signed out (IE-3 records it). The board's setup link (IE-1) is only for logins that never enrolled.
