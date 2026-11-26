# Milestone 2 summary: community operations (overnight build)

Built 2026-11-25 on branch `claude/ecstatic-bell-cnrpzi`, in small commits.
608 automated tests; ruff, bandit, pip-audit and the migration check are clean.
A real-browser pass over 51 pages with the full demo data found no errors and
no Content-Security-Policy violations.

## What was built

| Area | What you can do |
|---|---|
| Community definition | Community settings page: every Declaration/bylaw/board number (dues, late fee, invoice threshold, ARC and violation deadlines, meeting notice, records, resale, voting) with its citation; items missing a value or citation are flagged on the board page. Fiscal year (January). Common areas. |
| Manager role | Management-company staff can enter invoices, work orders, violations, ARC intake, documents, payments. They never approve, decide or vote. |
| Owner profile and directory | Contact details, consent to email notices (timestamped), opt-in directory listing, field by field. |
| Announcements and calendar | Members or board-only; urgent flag; emails carry only the title and a link. |
| Documents | Library with owner, board-only or one-lot visibility; versions kept forever; hostile-file checks; malware-scan hook; every download audited. |
| Meetings | Scheduling, notice by email to owners who consented (the rest listed for paper notice), shortfall warning against the notice rule, minutes from the library, executive sessions hidden from owners. |
| Resolutions and policies | Permanent register; changes supersede, never edit. |
| Records requests | Owner requests with due dates from the rule; board/manager respond. |
| Voting | Informal polls (one per owner) and formal votes (one per lot; quorum and threshold frozen with citations when opened; labelled not binding until counsel review); ballots can never change; secret ballots. |
| Architectural review | Full workflow with completeness check, review clock, deemed approval (only if the rule says so), recusal, appeal to the board, decision letters citing the section, every plan/photo version kept. |
| Violations | Notice with cure-by date, owner written response, hearing, board outcome, photos, notice letter. No fines. |
| Vendors and work orders | Vendor records with insurance-expiry warnings; work orders by common area. |
| Invoice approval | Under $500 one board approval; $500 and over two different board members; whoever entered it never approves; payments recorded with check numbers only. |
| Owner ledger | Monthly dues on the 1st (button, or automatic if you turn it on), payments, credits, reversals; prepayments drawn down month by month; owners see their own account and statements. |
| Collections | Overdue list and case tracking (reminders, payment plan, board-only referral). Nothing automatic. |
| Budgets | Categories, adopted budgets locked, budget vs actual by month. |
| Resale certificates | Requests, deadline and fee from rules, fee posted on issue, PDF certificate. |
| Online payments | Disabled "Pay online" button and a clean place to plug in Stripe later. |
| Reports | ARC, collections, open invoices, owner transfers, paid invoices by vendor, violations (with photos), work orders, AR aging, budget, budget spread. Each viewable and downloadable as CSV. |

## What I need from you

1. **Governing rules**: Board → Community settings. Replace every
   "DEMO - replace" value with the real number and its Declaration/bylaw
   section. Most important: monthly dues amount, ARC review days and
   whether deemed approval applies, violation cure days, meeting notice
   days, resale fee and deadline, voting quorum and pass threshold.
2. **Late fees** (decided 2026-11-25, D21; this note is kept as history):
   the board posts the fee from the late list with a button while
   `LEDGER_LATE_FEES_ENABLED` is true (the default, an emergency off
   switch on the server); automatic posting happens only when the board
   switches on the `late_fee.auto_apply` rule (off by default).
3. **Counsel review** before real use of: formal votes, resale certificate
   contents (68 Pa.C.S. § 5407), violation notice wording and hearing
   process, collections referral steps.
4. **Budget**: real expense categories and this year's budget lines.
5. **Stripe**: whether and when to add online payments (an HOA-owned
   Stripe account would be needed).
6. **Email**: the Hostinger mailbox for notices and invitations.

## Updating the POC

On the VM console:

```bash
cd /opt/hoa-portal && git pull --ff-only && sudo bash infra/poc/start-portal.sh
```

If you already ran `seed_demo` before this update, add the new demo data once:

```bash
docker compose -f docker-compose.yml -f docker-compose.tunnel.yml exec web python manage.py seed_demo_operations
```

## Honest limits

- Built overnight without your review; everything is tested, but please
  click through it before showing the board.
- Formal votes, violation hearings, collections and resale certificates
  carry legal weight: the software tracks them, counsel should check the
  process and wording.
- The full test suite now takes about two minutes, mostly the cross-owner
  URL matrix (every page tested against another owner's data).
- No owner-facing maintenance requests (item 7 was not selected); work
  orders are board/manager only.
