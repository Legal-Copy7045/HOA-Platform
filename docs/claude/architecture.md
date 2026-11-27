# Architecture rules

Moved from CLAUDE.md when it was cut to its essentials (D90): the same rules and reference, unchanged, read when a task touches this area. A rule here binds exactly as one in CLAUDE.md, which stays the source of truth (if a task conflicts with either, stop and ask).

## Architecture rules (non-negotiable)

1. Modular monolith. Apps: `accounts`, `community`, `audit`, `comms`, `documents`, `governance`, `arc`, `compliance`, `operations`, `ledger`, `payments`, `reports`. Apps call each other through service functions in `services.py`, never by importing another app's models for writes.
2. All configuration comes from environment variables (12-factor). No host-specific paths, hostnames, or secrets in code. Provide `.env.example`.
3. Portable by design: the same image must run on a Proxmox VM, a VPS, or a PaaS with only environment changes.
4. Single-tenant. No tenancy columns.
5. Anything financial, legal, or audit-related is append-only: ledger entries, ARC decisions, votes, audit events. Corrections are new reversing records, never edits or deletes. Enforce at the model layer (no update/delete paths), with a DB trigger, and by revoking UPDATE/DELETE/TRUNCATE from the runtime role (register the table with the append-only helper).
6. Rules that come from the Declaration (review deadlines, required submittals, voting thresholds, fee amounts) are data/configuration, not hard-coded, and each carries a citation field (e.g. "Declaration §X.Y").
7. Money is integer cents. Never floats.
