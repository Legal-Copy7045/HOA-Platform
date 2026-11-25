# Security review, November 2026

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
| A2 | An ARC reviewer who owned an affected neighbouring lot could **clear the affected-lots list**, stop being recused, and decide the request. | A recused person can never change the list. Lots can only be **added** once review starts. The list is frozen after a decision, and every change is audited by lot number. |
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
