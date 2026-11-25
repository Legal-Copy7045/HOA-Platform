# Milestone 2 plan: community operations

Directed by the board representative on 2026-11-25 for an overnight build.
Everything here follows CLAUDE.md: scoped access, services as the only write
path, audit events, append-only records for anything financial, legal or
decision-related, rules from the Declaration as data with citations, money
in integer cents, synthetic data only.

## Decisions taken with the user

| Topic | Decision |
|---|---|
| Dues | Monthly charge due on the 1st (covenants). Annual billing optional. Prepayments sit as credit and are drawn down as charges post. Amounts are data entered by the board, never code. |
| Fiscal year | Starts January 1 (setting). |
| Late fee | Current fee ($6) is recorded as a rule but **never applied automatically** until PA law is reviewed with the user. |
| Invoice approval | Under $500: one board approval. $500 and over: two different board approvals. The person who entered an invoice never approves it. Threshold is a rule. |
| Manager role | New role for management-company staff: data entry and workflow steps (invoices, work orders, violations, ARC intake, documents). Never approves, never votes. |
| Common areas | Stormwater/ponds; entrance and landscaping (data, editable). |
| Voting | One module, two modes: informal polls (non-binding) and formal votes (one vote per lot, immutable ballots, quorum/threshold rules with citations, labelled "not for binding use until counsel reviews"). |
| Work orders | Included with vendors (needed by the reports and invoices). |
| Collections | Tracking and reminder steps only; no automatic fees, liens or legal steps. |
| Stripe | Placeholder only: disabled "Pay online", an interface to plug in later, no keys. |

## Community definition (`community`)

- `CommunityProfile` (single row): legal name, fiscal-year start month.
- `GoverningRule`: key from a fixed registry, value, citation, notes, with
  history. Every Declaration-derived number (ARC review days, resale fee and
  deadline, records-request days, meeting notice days, voting quorum and
  threshold, late fee, invoice threshold) lives here. Missing rules are
  listed on the board dashboard.
- `CommonArea`: name, kind, notes, active.

## Shared workflow engine (`core.workflow`)

Each workflow object has a state; allowed transitions are declared once
(from-state, action, to-state, permission, guard). Every transition writes an
append-only `WorkflowEvent` (who, when, from, to, note) and an audit event.
Used by ARC requests, invoices, work orders, violations, records requests and
resale certificates.

## Apps and features

| App | Features |
|---|---|
| `community` | profile, governing rules, common areas, owner profile/contact preferences, opt-in directory |
| `comms` | announcements (read tracking, urgent flag, email), calendar/events |
| `documents` | library: owner/board visibility, immutable published versions, type/size allowlist, scan hook, authorized downloads |
| `governance` | meetings (notice, agenda, minutes, attendance), resolutions and policy register (append-only), records requests, votes (polls and formal) |
| `arc` | architectural requests: draft, submitted, completeness check, in review, decided, appeal, completed; deadlines and deemed approval from rules; recusal; decision letter |
| `compliance` | violations: report, notice, owner response, hearing, resolved; photos |
| `operations` | vendors, work orders, AP invoices with approval workflow, board tasks |
| `ledger` | owner accounts, assessment schedule, charges, payments, credits, prepayment drawdown, reversing corrections, collections tracking, resale certificates, budgets |
| `payments` | Stripe placeholder interface |
| `reports` | ARC, collections, open invoices, ownership transfers, paid invoices by vendor, violations (with photos), work orders, AR aging, budget, budget vs actual by month; each viewable and downloadable as CSV |
| `accounts` | owner data export |

## Build order

1. Community definition, manager role, workflow engine.
2. Owner profile, directory, calendar, announcements, board tasks, exports.
3. Documents.
4. Governance.
5. ARC, violations, vendors, work orders, invoice approval.
6. Ledger, dues, Stripe placeholder, budgets, collections, resale.
7. Reports, demo data, documentation.

## Needed from the user (collected in the morning summary)

- Governing-rule values and citations from the Declaration and bylaws.
- Dues amount; budget lines.
- Late-fee review against PA law before any automatic use.
- Counsel review of formal voting rules before binding use.
