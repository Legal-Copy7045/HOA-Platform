# Glossary: one name for each thing

The portal uses one name for each thing, the same in the menu, the page title
(`<h1>`), dashboard tiles, emails and letters. Owners see plain words; the code
may use shorter or older names (right-hand column). When you add a page, use the
name here, and make its menu label match its `<h1>` (a test checks this).

| What owners and the board see | Meaning | Name in code |
|---|---|---|
| Dashboard | The home page after sign-in: to-dos first, then numbers. | `home`, `core/dashboard.py` |
| Home, address | A property in the community, always named by its street address, e.g. 165 Imaginary Orchard Way. The plat lot number is not used as a name (D40): it shows only as a small line under a home's title, in brackets on formal documents ("165 Imaginary Orchard Way (Lot 30)": resale certificate, statements and delinquency letters, ARC decision letters, violation notices, the printed neighbor acknowledgement form), and as a last "Lot number" column in the roster, accounting and finance exports. The board can still search and filter by it. | `Lot` (`Lot.label`, `Lot.legal_label`) |
| Home account | The board's page for one home's money: entries, payments, charges and letters. | `lot_ledger` |
| Home neighbors | Each home's left and right neighbors from the development plan, and other homes a project there affects. | `arc_neighbors` |
| Menu sections | **My home** (your own home, account and requests), **Community**, **Association** (the board's work), **Maintenance** (work orders, vendors, contracts), **Finance** and **Admin**. The owner side and the board side stay separate sections. | `core/navigation.py` `SECTIONS` |
| Needs your attention / My items | Everything waiting on you. The dashboard list, the My items page and the menu bubbles all come from one list. Set-up warnings (fewer than two people able to act, board terms past their end, rules missing a citation) sit at the top of My items. | `core.my_items.items_for` |
| Second approvals | Requests a second board member must approve (board seats, sysadmin nominations, sign-in resets, account changes). Reached from My items, where these to-dos are grouped as "Second approvals" (account changes under "Homes and owners"). | `approvals` |
| Preview as owner | A board member or manager sees the portal as one home's owner does, read-only and logged. Started from the home's page. | `core/preview.py` |
| Search | The search box at the top of the menu and its results page: finds announcements, documents, meetings, requests and more, grouped by area. Shows only what you can already open. | `core/search.py`, `<app>/search.py` |
| My home | Your home (its address as the title, the plat lot number small beneath), the people on your account. | `my_lot` |
| Assessment | The regular monthly charge, named as the covenants name it: a Community settings value ("What the regular monthly charge is called", with its citation; default Assessment, the Act's word). Pages, emails, letters and new charges use it; the code still says "dues". | `charge.term`, `community/terms.py`, `{% term %}` |
| Monthly statement email | Sent when the month's assessment is posted, to owners and authorized or view-only contacts who leave it ticked on My profile: amount due, what to do, links, the statement PDF. | `ledger/statement_email.py`, `StatementEmail` |
| My account | Your statement and balance. The board's view of every home is **Owner accounts**; one home's page is its **Home account**. | `ledger` |
| Open charges | Each charge on an account (assessments, late fees, other charges, resale fees) with what is still unpaid on it and the payments applied to it. | `ledger.allocation`, `PaymentApplication` |
| Invoice 2026-0142 / Assessment – March 2026 | A charge's number (year and sequence) and its name. | `ChargeNumber` |
| Pay online | An owner or authorized contact pays chosen charges on Stripe's hosted page: bank account or debit card (no fee), or credit card (with the surcharge). | `pay_online`, `payments.checkout` |
| Payment status | The page Stripe returns the payer to; the portal asks Stripe itself whether the payment went through. | `pay_return` |
| Credit card surcharge | The fee on credit card payments only (never bank or debit), at most 3%. Not part of the account balance. | `payments.credit_card_surcharge_bp` |
| Pay assessments automatically | Automatic payments: each monthly assessment (never late fees or other charges) is paid on its due day from a bank account (no fee) or card (credit cards with the surcharge) the payer saved on Stripe's page. Turned off any time on My account; ends when the home is sold. | `autopay`, `payments.autopay` |
| Online payments | The board's page of Stripe payments, bank payments processing, problems to handle, payouts and the daily check against Stripe. | `online_payments`, `payments.sync` |
| Account number | The number of one ownership period of a home (changes when the home is sold). Shown as plain text, not a badge. | `Account.number` |
| My home improvement requests (ARC) | Asking the Architectural Review Committee to approve an exterior change, and neighbors' projects you are asked to acknowledge. The board's list is **Architectural review**. | `arc_list?mine=1`, `ArcRequest` |
| Neighbor acknowledgement | A neighbor confirms they have seen a project. It never decides the request. | "signoff" (`arc/signoffs.py`) |
| Notices about my home | Covenant or rule notices sent to an owner. The board's list is **Violations**. | `compliance`, `Violation` |
| Fix by | The date an owner should fix a notice by. | `cure_by` |
| Official notices | The formal notices the association must send every owner on the deed (meeting, budget and assessment notices, delinquency letters, others the Declaration or law requires), by email to those who agreed, by post to everyone else. Not the same as **Notices about my home**. | `notice_choice`, `community/notices.py` |
| How we reach you | The card on My profile with the official notices choice, emails, phones (and opt-in text alerts) and the one mailing address for post. | `my_profile` |
| Mailing address for post | Where paper notices and letters go: the home's address or one address the person enters. "Address on file (please check)" is one typed before the separate fields existed. | `ContactProfile.mail_*`, `postal_address` |
| Text alerts | Optional texts about urgent community matters, only after ticking the box and entering a texted code. | `comms/sms.py` |
| Approved automatically | No decision was made within the review period, so the Declaration treats the request as approved. | "deemed approval" |
| Can't take part in the review | A reviewer who is the applicant or owns an affected home. | "recused" (`is_recused`) |
| Checked for completeness | The board checks a submitted request has everything it needs, then the review clock starts. | `submitted` state |
| Request #12, Notice #7, Work order #3 | Record numbers. No letter prefixes (ARC-12, V-7, WO-3, R-2). | primary key |
| Version 2 | A document's second upload. | `DocumentVersion.number` |
| My requests | Questions and requests to the board. The board's list is **Owner requests**. View-only contacts see the requests on the account they follow, read-only; former owners find theirs in **My history**. | `operations.OwnerRequest` |
| Records requests | An owner's request to inspect association records (Pennsylvania law). The owner's own list is **My records requests**. | `governance.RecordsRequest` |
| Voting | One list of **Votes**, **Elections** and **Surveys**, filtered by open or past and by type. Board members start new ones here (New vote, New election, New survey). Owners also find **Give a proxy** for coming owners' meetings. The old addresses of the three lists lead here. | `voting` |
| Votes | Formal votes of the owners: binding, one ballot per home, with the quorum and pass rules (and citations) fixed when the vote opens. Informal polls made before November 26, 2026 stay readable under Past; a new poll is a survey with one question. | `Vote` |
| Elections | Board elections: nominations, ballots, results. | `Election` |
| Surveys | Informal multi-question surveys from the board (not a vote). Anonymous ones keep answers apart from who responded. Someone who can only answer surveys (a tenant) sees the Voting list as **Surveys**, in the menu and the heading. | `Survey` |
| Survey results | Totals for each question; written answers are seen by the board only. Owners see totals after it closes, if the board shares them. | `survey_results` |
| Proxy | Someone you choose to vote for your home at a meeting. Holders are picked by name (board members) or by address (owners). | `Proxy` |
| Resolutions and policies | Board resolutions and the policies in force. The board also adopts a resolution by written consent here and sees those **Waiting for signatures**. | `Resolution` |
| Adopt by written consent | A board decision without a meeting: every serving board member must approve in writing; one decline and it is not adopted. The signatures are kept as their own records; once adopted it is recorded as a resolution. | `ConsentRequest`, `consents` (redirects to Resolutions and policies) |
| Meeting page | Everything about one meeting: agenda, notice, minutes (uploaded here), packet, the votes, elections and resolutions that belong to it, quorum and proxies. Meetings put themselves on the calendar and come off it when cancelled; executive sessions never appear there. | `meeting_detail` |
| Meeting packet | One PDF for a meeting: cover, agenda, minutes, financial totals and an attachment index, with a .zip of the attachments. The **Board packet** (marked "Confidential – board only") adds receivables and open-item counts; the **Owner packet** never has delinquency, violation or collection details. | `governance/packets.py`, `MeetingPacket` |
| Community info | One page for **Frequently asked questions**, **Board and committees**, the **Owner directory** and (for committee members) **Committees**. With only one of these open to you, the menu goes straight to it. | `community_info` |
| Reserve plan | The board's in-house reserve study: the things the association will one day replace (component, cost today, useful life, years left), a year-by-year projection of the reserve fund, percent funded and the contribution needed. Finance menu. | `reserve_plan`, `ledger/reserves.py` |
| Record as adopted | The board fixes the current reserve plan's figures as adopted on a date ("reserve plan adopted Nov 26, 2026"); kept unchanged. | `ReserveStudySnapshot` |
| Budget and reserves | What owners see of the money plan: the budget (income, expenses, the monthly assessment per home) and the adopted reserve plan's summary (percent funded, adopted date, next major items; no board notes). The year is in the subtitle. The old Reserve plan summary page redirects here. | `owner_budget` (`reserve_summary` redirects) |
| Percent funded | The reserve balance compared with the fully funded balance (each component's cost times the share of its life already used). | `percent_funded` |
| Settings | One page for rarely used set-up pages: Community settings, ARC request types, Home neighbors, Letters and certificates, Common area booking rules, Branding, Features, Contact retention. With only one open to you, the menu goes straight to it. | `settings_hub` |
| Contact retention | Former owners' contact details cleared after the retention period. | `retention` |
| Server tasks | Admin page where a sysadmin starts a backup, a restore drill, the off-site lock check, re-applying host security or the health checks; the server's own agent runs them from a fixed list. The board sees the history. | `server_tasks`, `core/host_tasks.py`, `infra/poc/host-tasks.sh` |
| Access review | The board's quarterly check of every login's access, on the People page. | `access_review` |
| Homes and owners | Every home, by address, with its current owners; the "Accounts, contacts and balances" view adds each account's other people, contact details and balance, with the CSV export (was Roster); the board's "Sign-ups" view shows who has signed up, home by home, how homes get official notices, "Invite everyone not yet invited" and reminders, and every invitation with re-send and revoke (was Invitations and sign-ups, D50). | `lot_list` (`roster` redirects; `?view=signups`, `onboarding` redirects there) |
| Invitations | The sysadmin's list of invitations with re-send and revoke, under Admin. No register of owners (D9). The board's sign-ups are on **Homes and owners**. | `invitations` (the board is sent to the Sign-ups view) |
| Ask to change the owners on the deed | On My home, an owner asks the board to add an owner (joint owner, trust or entity) or remove one, with the recorded deed attached. A board member who is not the requester or the person added or removed approves; an added person with an email is invited to sign in. (Contacts: "Ask to add someone".) | `AccountChangeRequest` (`add_owner`, `remove_owner`), D41 |
| Board and committees | Who serves: board members with their offices, and each committee with its members. Names and offices only, for every resident. | `board_and_committees` |
| Common area booking rules | Each common area (name, kind, in use) and its booking rules, edited together, with where the rules come from. | `reservation_settings` |
| Recurring deadlines | The Board tasks tab for filings and renewals the association must not miss (insurance, taxes, annual report), each with a reminder window (was Compliance calendar). | `tasks?tab=deadlines`, `ComplianceItem` (`compliance` redirects) |
| Passkeys and security | Your sign-in methods (passkey, authenticator app, recovery codes). | allauth MFA |
| Download my data | A file with everything the portal holds about you, for your records. | JSON export |
| RSVP | Your answer to a calendar event: Going, Maybe or Not going, with guests if the event allows. Other owners see counts only. | `comms.EventRsvp`, `comms/rsvp.py` |
| Volunteers | Jobs to sign up for at an event ("Bring a dessert"), each with a number of places. | `VolunteerSlot`, `VolunteerSignup` |
| Events I'm going to | Dashboard list of coming events you said Going to or volunteer at. | `comms.rsvp.my_upcoming` |
| Bank feed | Lines from the association's bank (through Teller, or imported statement files), matched to deposits and vendor payments for the monthly reconciliation. Board, manager and auditor only. | `ledger/bankfeeds`, `BankTransaction` |
| Connect a bank | Linking the association's bank account through Teller (board only, after a fresh password or passkey check). | `bank_connect`, Teller Connect |
| Match the columns | The step after uploading a CSV statement: which column is the date, description and amount. | `bank_import` (CSV mapping) |

Dates: "Nov 3" for a date in the current month, "Nov 3, 2027" for any other
(`{{ d|short_date }}`, `core.dates.short_date`).
