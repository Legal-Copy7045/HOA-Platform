# Glossary: one name for each thing

The portal uses one name for each thing, the same in the menu, the page title
(`<h1>`), dashboard tiles, emails and letters. Owners see plain words; the code
may use shorter or older names (right-hand column). When you add a page, use the
name here, and make its menu label match its `<h1>` (a test checks this).

| What owners and the board see | Meaning | Name in code |
|---|---|---|
| Dashboard | The home page after sign-in: to-dos first, then numbers. | `home`, `core/dashboard.py` |
| Needs your attention / My items | Everything waiting on you. The dashboard list, the My items page, the Board overview counts and the menu bubbles all come from one list. | `core.my_items.items_for` |
| My home | Your lot, the people on your account. | `my_lot` |
| My account | Your dues statement and balance. The board's view of every lot is **Owner accounts**. | `ledger` |
| Account number | The number of one ownership period of a lot (changes when the lot is sold). Shown as plain text, not a badge. | `Account.number` |
| Home improvement approvals (ARC) | Asking the Architectural Review Committee to approve an exterior change. The board's list is **Architectural review**. | `arc`, `ArcRequest` |
| Neighbor acknowledgement | A neighbor confirms they have seen a project. It never decides the request. | "signoff" (`arc/signoffs.py`) |
| Notices about my home | Covenant or rule notices sent to an owner. The board's list is **Violations**. | `compliance`, `Violation` |
| Fix by | The date an owner should fix a notice by. | `cure_by` |
| Approved automatically | No decision was made within the review period, so the Declaration treats the request as approved. | "deemed approval" |
| Can't take part in the review | A reviewer who is the applicant or owns an affected lot. | "recused" (`is_recused`) |
| Checked for completeness | The board checks a submitted request has everything it needs, then the review clock starts. | `submitted` state |
| Request #12, Notice #7, Work order #3 | Record numbers. No letter prefixes (ARC-12, V-7, WO-3, R-2). | primary key |
| Version 2 | A document's second upload. | `DocumentVersion.number` |
| My requests | Questions and requests to the board. The board's list is **Owner requests**. | `operations.OwnerRequest` |
| Records requests | An owner's request to inspect association records (Pennsylvania law). | `governance.RecordsRequest` |
| Voting | One page for **Votes and polls** and **Elections**. | `voting` |
| Votes and polls | Formal votes (binding, one ballot per lot) and informal polls (one per owner). | `Vote` |
| Elections | Board elections: nominations, ballots, results. | `Election` |
| Proxy | Someone you choose to vote for your lot at a meeting. Holders are picked by name (board members) or by address (owners). | `Proxy` |
| Resolutions and policies | Board resolutions and the policies in force. | `Resolution` |
| Community info | One page for **Frequently asked questions**, the **Owner directory** and **Committees**. | `community_info` |
| Settings | One page for rarely used set-up pages: Community settings, ARC request types, Lot neighbors, Letters and certificates, Compliance calendar, Branding, Features. | `settings_hub` |
| Passkeys and security | Your sign-in methods (passkey, authenticator app, recovery codes). | allauth MFA |
| Download my data | A file with everything the portal holds about you, for your records. | JSON export |
| Bank feed | Lines from the association's bank (through Teller, or imported statement files), matched to deposits and vendor payments for the monthly reconciliation. Board, manager and auditor only. | `ledger/bankfeeds`, `BankTransaction` |
| Connect a bank | Linking the association's bank account through Teller (board only, after a fresh password or passkey check). | `bank_connect`, Teller Connect |
| Match the columns | The step after uploading a CSV statement: which column is the date, description and amount. | `bank_import` (CSV mapping) |

Dates: "Nov 3" for a date in the current month, "Nov 3, 2027" for any other
(`{{ d|short_date }}`, `core.dates.short_date`).
