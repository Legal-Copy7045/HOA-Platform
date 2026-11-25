# Milestone 3 plan: accounts, requests, committees, ARC sign-off

Directed 2026-11-25 after comparing the portal with the management company's
current portal (screenshots reviewed in chat; no real data copied). Numbers
refer to the gap list in that review. Each phase ships with tests (including
cross-owner and former-owner access), regression catalogue entries and a
commit.

## Decisions taken with the owner

- **Accounts (4, 5).** An *account* is one ownership period of one lot: it
  has an account number, the lot, and is **active** or **dormant** (closed
  when the lot is sold). The lot's history stays on the lot. People attach
  to an account with a kind:
  - **owner** (on the deed; several allowed): everything an owner can do;
  - **authorized contact** (e.g. spouse not on the deed, property manager):
    sees the account and raises requests; no votes, no ARC applications;
  - **view-only contact** (e.g. an adult child): sees the account and
    notices; cannot act;
  - **tenant**: community documents, announcements, rules and their own
    requests; no balance, no votes.
  Each person has extra contact methods (phones, emails, addresses), each
  with its own notice consent.
- **Former owners (changes a CLAUDE.md rule).** After a sale the owners'
  logins stay usable **read-only, for their own records only**: their
  account statements and history, and their ARC requests, notices and
  requests from their time. No current documents, announcements, directory
  or anything after the sale; nothing can be raised or changed.
- **Owner requests and threads (1, 2).** Owners (and authorized contacts,
  tenants for their own) raise general questions, billing questions and
  maintenance/common-area reports. Each request, ARC request, violation and
  work order has a message thread; emails carry only a link. Maintenance
  requests can become work orders.
- **SLAs and follow-ups (11).** The board sets a response target per request
  type (business days); overdue items are flagged on the board home and in
  "My items". Violations, collections and work orders get a follow-up date.
- **My items queue (10, 16).** One queue of everything waiting on the
  signed-in person (approvals, ARC reviews, requests, SLAs, follow-ups) with
  counts per area on the board home. Real workflow steps, not just comments.
- **Committees (13).** The board creates committees (social, landscaping,
  ARC...), with members and a chair. Each has a space: discussion threads,
  a document folder, tasks and events. The directory gets Board and
  Committee tabs.
- **Documents (12).** Board-managed folders with per-folder visibility
  (owners, board, a committee) and search. **No public documents.**
- **Recurring invoices (9).** A recurring invoice is tied to a board-approved
  contract (vendor, amount cap, schedule, resolution or citation); each
  occurrence within the cap is pre-approved and audited.
- **Roster, list tools (7, 8).** One board roster (account number, owners,
  address, contact, balance, tags... previous accounts on request) with CSV
  export; open/closed, mine, date range, export and paging on the lists.
- **Upcoming charges (14), FAQs (3), welcome message (17).**
- **AutoPay (15).** Stripe supports saved payment methods and recurring
  charges through its hosted pages, so the card never touches the portal;
  built when the Stripe decision is made (placeholder until then).
- **ARC neighbor sign-off.**
  - Neighboring lots come from an adjacency list the board records once
    from the development plan (per lot: its neighbors); they are
    pre-selected on each request and the board can add other addresses.
  - Neighbors with a portal login sign in the portal; anyone else gets a
    **single-use emailed link** (expires) showing only that request's summary
    and plans, and signs by typed name with approve/object and a comment.
  - A **printable form** prefilled from the request (applicant, lot, project,
    neighbors with signature lines) can be printed, signed on paper,
    scanned and uploaded back.
  - **Gemini** reads an uploaded signed form and proposes the sign-offs
    (who, approve/object, date). **Human in the loop by default**: the board
    or manager confirms; a board setting can make it automatic. The AI never
    decides the ARC request: that stays with the committee, with recusal.
    The free Gemini tier may use submitted content to improve Google's
    products; a paid key is recommended once real owner data is involved.

## Phases

1. Accounts, account people, contact methods, former-owner read-only access,
   account numbers, upcoming charges.
2. Owner requests with SLA targets, message threads, conversion to work
   orders, follow-up dates, My items queue.
3. Committees and their spaces; directory tabs.
4. Document folders and search.
5. Recurring invoices against approved contracts.
6. Board roster and list tools; FAQs; welcome message.
7. ARC: neighbor adjacency, sign-off requests (portal and one-time link),
   printable form, scanned upload with Gemini proposals.
