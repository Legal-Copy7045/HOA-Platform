"""Registry of audit actions and the metadata each may carry (decision D7).

Metadata is restricted to the listed fields so personal data cannot creep into
the audit log. Values must be short scalars. Later steps add their actions
here, next to the code that records them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str
    metadata_fields: frozenset[str] = field(default_factory=frozenset)


def _spec(name: str, description: str, *fields: str) -> ActionSpec:
    return ActionSpec(name, description, frozenset(fields))


_SPECS = [
    _spec(
        "audit.ip_purged",
        "Expired IP addresses removed from audit records",
        "purged",
        "retention_days",
    ),
    # community (step 3). IDs and dates only; no names, emails or addresses.
    _spec("person.recorded", "Person added to the community register", "person_id"),
    _spec(
        "ownership.recorded",
        "Ownership recorded",
        "lot_id",
        "person_id",
        "ownership_type",
        "start_date",
    ),
    _spec("ownership.ended", "Ownership ended", "lot_id", "person_id", "end_date"),
    _spec(
        "ownership.transferred",
        "Lot transferred to new owners",
        "lot_id",
        "transfer_date",
        "ended_count",
        "new_owner_count",
    ),
    _spec(
        "seat.recorded", "Board seat recorded", "person_id", "seat_type", "term_start", "term_end"
    ),
    _spec("seat.ended", "Board seat ended", "person_id", "ended_on", "reason"),
    _spec("office.recorded", "Office holding recorded", "person_id", "office", "start_date"),
    _spec("office.ended", "Office holding ended", "person_id", "office", "end_date"),
    _spec(
        "role.granted",
        "Role granted",
        "person_id",
        "role",
        "start_date",
        "appointed_by",
    ),
    _spec("role.ended", "Role ended", "person_id", "role", "end_date"),
    # sign-in (step 5)
    _spec("auth.login", "Signed in", "method"),
    _spec("auth.logout", "Signed out"),
    _spec("auth.login_failed", "Failed sign-in"),
    _spec("auth.lockout", "Sign-in locked out after repeated failures"),
    _spec("mfa.authenticator_added", "Authenticator added", "type"),
    _spec("mfa.authenticator_removed", "Authenticator removed", "type"),
    _spec("mfa.recovery_codes_regenerated", "Recovery codes regenerated", "type"),
    _spec("password.changed", "Password changed or set"),
    _spec("password.reset", "Password reset by email link"),
    # invitations (step 6)
    _spec("person.login_linked", "Login linked to a person", "person_id", "user_id"),
    _spec("invite.created", "Invitation created", "person_id"),
    _spec("invite.resent", "Invitation re-sent", "person_id"),
    _spec("invite.revoked", "Invitation revoked", "person_id"),
    _spec("invite.sent", "Invitation email sent", "person_id", "send_count"),
    _spec("invite.accepted", "Invitation accepted", "person_id"),
    # two-person flows (step 7)
    _spec("role.sysadmin_nominated", "Sysadmin nominated", "person_id"),
    _spec("role.sysadmin_approved", "Sysadmin nomination approved", "person_id"),
    _spec("role.sysadmin_declined", "Sysadmin nomination declined or withdrawn", "person_id"),
    _spec("mfa_reset.started", "Sign-in reset started", "user_id", "verification_method"),
    _spec("mfa_reset.approved", "Sign-in reset approved", "user_id"),
    _spec("mfa_reset.cancelled", "Sign-in reset cancelled", "user_id"),
    _spec("mfa_reset.code_redeemed", "Enrollment code used", "user_id"),
    _spec("mfa_reset.code_failed", "Wrong enrollment code entered", "user_id", "attempts"),
    # sysadmin visibility (D9)
    _spec("sysadmin.request", "Portal request by a sysadmin", "view", "method", "status"),
    _spec("community.rule_changed", "Governing rule changed", "key", "has_citation"),
    _spec("community.profile_changed", "Community profile changed"),
    _spec("community.common_area_saved", "Common area saved", "kind", "active"),
    _spec(
        "profile.updated",
        "Own profile updated",
        "notice_consent",
        "consent_changed",
        "in_directory",
    ),
    _spec("export.own_data", "Downloaded own data"),
    _spec("preview.owner_started", "Homeowner view started", "lot_id"),
    _spec("account.number_set", "Account number set"),
    _spec("account.contact_added", "Account contact added", "person_id", "kind"),
    _spec("account.contact_ended", "Account contact ended", "person_id", "kind"),
    _spec("contact.method_added", "Contact detail added", "kind"),
    _spec("contact.method_removed", "Contact detail removed", "kind"),
    _spec("request.submitted", "Owner request submitted", "type", "lot_id"),
    _spec("request.assigned", "Owner request assigned", "person_id"),
    _spec("request.work_order_created", "Work order created from a request", "work_order_id"),
    _spec("thread.message_posted", "Thread message posted", "kind", "object_id", "internal"),
    _spec("followup.set", "Follow-up date set", "kind", "object_id", "on"),
    _spec("account.change_requested", "Account change requested", "action", "kind"),
    _spec("account.change_decided", "Account change decided", "action", "decision"),
    _spec("account.change_withdrawn", "Account change withdrawn", "action"),
    _spec("arc.project_type_saved", "ARC project type saved", "active"),
    _spec(
        "register.imported", "Lot and owner register imported", "lots", "owners", "people_created"
    ),
    _spec("contract.entered", "Contract entered", "amount_cap_cents", "schedule"),
    _spec("contract.ended", "Contract end date set", "ends_on"),
    _spec(
        "invoice.contract_preapproved",
        "Invoice pre-approved under a contract",
        "contract_id",
        "period",
    ),
    _spec("faq.saved", "FAQ entry saved", "published"),
    _spec(
        "ledger.late_fees_posted", "Late fees posted", "period", "lots", "amount_cents", "automatic"
    ),
    _spec("seat.proposed", "Board seat proposed", "person_id", "seat_type"),
    _spec("seat.proposal_decided", "Board seat proposal decided", "person_id", "decision"),
    _spec("arc.neighbors_set", "Lot neighbors set", "lot_id", "count"),
    _spec("arc.signoffs_prepared", "Neighbor sign-offs prepared", "count"),
    _spec("arc.signoff_added", "Sign-off added", "kind"),
    _spec("arc.signoff_withdrawn", "Sign-off withdrawn"),
    _spec("arc.signoffs_sent", "Sign-off requests sent", "portal", "link", "no_contact"),
    _spec("arc.signoff_answered", "Neighbor sign-off answered", "position", "channel"),
    _spec("arc.scan_reading_requested", "Signed form sent for reading"),
    _spec("arc.scan_reading_done", "Signed form read", "proposals", "automatic"),
    _spec("election.created", "Election created", "seats"),
    _spec("election.nominated", "Candidate nominated", "person_id"),
    _spec("election.candidate_accepted", "Nomination accepted", "candidate_id"),
    _spec("election.candidate_withdrawn", "Candidate withdrawn", "candidate_id"),
    _spec("election.voting_opened", "Election voting opened"),
    _spec("election.voting_closed", "Election voting closed"),
    _spec("election.ballot_cast", "Election ballot cast", "lot_id", "by_proxy"),
    _spec("election.certified", "Election certified", "ballots", "tie"),
    _spec("meeting.proxy_granted", "Proxy granted", "lot_id"),
    _spec("meeting.proxy_revoked", "Proxy revoked", "lot_id"),
    _spec("consent.proposed", "Written consent proposed", "signers"),
    _spec("consent.signed", "Written consent signed", "approve"),
    _spec("consent.withdrawn", "Written consent withdrawn"),
    _spec("budget.notice_sent", "Budget notice sent", "stage", "emailed", "paper"),
    _spec("compliance.saved", "Compliance item saved"),
    _spec("compliance.done", "Compliance item done", "recurring"),
    _spec("accounting.setup_saved", "Accounting setup saved"),
    _spec("accounting.exported", "Accounting journal exported", "start", "end", "rows"),
    _spec("accounting.deposit_created", "Deposit recorded", "count", "total_cents"),
    _spec("accounting.deposit_voided", "Deposit voided"),
    _spec("accounting.reconciled", "Bank statement reconciled", "difference_cents"),
    _spec("accounting.1099_exported", "1099 vendor totals exported", "year"),
    _spec("collections.courtesy_sent", "Courtesy payment reminders sent", "count"),
    _spec("system.test_email_sent", "Test email sent", "ok"),
    _spec("feature.switched", "Feature switched on or off", "feature", "enabled"),
    _spec("community.letters_changed", "Letter and certificate settings changed"),
    _spec("ledger.letter_template_saved", "Letter wording saved", "kind"),
    _spec("ledger.notice_generated", "Statement or notice generated", "kind", "lot_id"),
    _spec("committee.created", "Committee created"),
    _spec("committee.retired", "Committee retired"),
    _spec("committee.reactivated", "Committee reactivated"),
    _spec("committee.member_appointed", "Committee member appointed", "person_id", "role"),
    _spec("committee.member_ended", "Committee membership ended", "person_id", "role"),
    _spec("committee.topic_started", "Committee topic started"),
    _spec(
        "branding.login_background_set", "Sign-in background uploaded", "variant", "width", "height"
    ),
    _spec("branding.login_background_removed", "Sign-in background removed", "variant"),
    _spec("preview.owner_stopped", "Homeowner view ended", "lot_id"),
    _spec("announcement.published", "Announcement published", "audience", "urgent"),
    _spec("event.created", "Calendar event created", "audience"),
    _spec("event.cancelled", "Calendar event cancelled"),
    _spec("task.created", "Board task created"),
    _spec("task.closed", "Board task closed", "status"),
    _spec("document.created", "Document created", "visibility", "category"),
    _spec("document.folder_created", "Document folder created", "visibility"),
    _spec("document.moved", "Document filed in a folder", "folder_id", "visibility"),
    _spec("document.version_uploaded", "Document version uploaded", "version", "scan"),
    _spec("document.upload_blocked", "Upload blocked", "reason"),
    _spec("document.archived", "Document archived"),
    _spec("document.restored", "Document restored"),
    _spec("document.downloaded", "Document downloaded", "version"),
    _spec("document.viewed", "Document page viewed"),
    _spec("meeting.created", "Meeting scheduled", "kind"),
    _spec("meeting.noticed", "Meeting notice sent", "emailed", "paper", "short_days"),
    _spec("meeting.status_changed", "Meeting held or cancelled", "status"),
    _spec("meeting.minutes_attached", "Minutes attached to meeting", "document_id"),
    _spec("meeting.attendance", "Attendance recorded", "person_id", "present"),
    _spec("resolution.recorded", "Resolution or policy recorded", "number", "kind", "supersedes"),
    _spec("records_request.submitted", "Records request submitted", "due_on"),
    _spec("vote.created", "Vote created", "kind", "options"),
    _spec("vote.opened", "Vote opened", "kind"),
    _spec("vote.closed", "Vote closed"),
    _spec("vote.ballot_cast", "Ballot cast", "lot_id"),
    _spec("attachment.added", "File attached", "owner_type", "scan"),
    _spec("attachment.downloaded", "Attached file downloaded", "owner_type", "scan"),
    _spec("arc.created", "ARC request started", "lot_id"),
    _spec("arc.affected_lots_set", "ARC affected lots recorded", "count", "added", "removed"),
    _spec("arc.position_recorded", "ARC reviewer position recorded"),
    _spec("arc.letter_downloaded", "ARC decision letter downloaded"),
    _spec("violation.reported", "Violation reported", "lot_id", "category"),
    _spec("violation.letter_downloaded", "Violation notice downloaded"),
    _spec("vendor.saved", "Vendor saved"),
    _spec("category.created", "Expense category created"),
    _spec("work_order.created", "Work order created", "priority"),
    _spec("work_order.vendor_set", "Work order vendor set"),
    _spec("invoice.entered", "Invoice entered", "amount_cents"),
    _spec("invoice.approval_given", "Invoice approval given", "have", "need"),
    _spec("invoice.payment_recorded", "Invoice payment recorded", "amount_cents", "method"),
    _spec(
        "ledger.assessments_posted", "Monthly assessments posted", "period", "lots", "amount_cents"
    ),
    _spec("ledger.payment_recorded", "Owner payment recorded", "lot_id", "amount_cents"),
    _spec("ledger.charge_posted", "Charge posted", "lot_id", "kind", "amount_cents"),
    _spec("ledger.credit_posted", "Credit posted", "lot_id", "amount_cents"),
    _spec("ledger.entry_reversed", "Ledger entry reversed", "lot_id", "reverses"),
    _spec("ledger.statement_downloaded", "Statement downloaded", "lot_id"),
    _spec("collection.opened", "Collection case opened", "lot_id", "balance_cents"),
    _spec("budget.created", "Budget created", "fiscal_year"),
    _spec("budget.line_added", "Budget line added", "kind", "annual_cents"),
    _spec("budget.adopted", "Budget adopted", "fiscal_year"),
    _spec("resale.requested", "Resale certificate requested", "lot_id"),
    _spec("resale.certificate_downloaded", "Resale certificate downloaded", "lot_id"),
    _spec("resale.disclosures_saved", "Resale disclosures saved"),
    _spec("report.viewed", "Report viewed", "report", "rows"),
    _spec("report.exported", "Report exported (CSV)", "report", "rows"),
    _spec("list.exported", "List exported (CSV)", "list", "rows"),
    _spec("invite.link_issued", "Invitation link printed on the server (bootstrap)", "person_id"),
    _spec(
        "system.bootstrap", "First officer created on the server", "person_id", "sysadmin_person_id"
    ),
    _spec(
        "sysadmin.breakglass",
        "Break-glass command run on the server",
        "command",
        "reason",
        "target_id",
    ),
]

ACTIONS: dict[str, ActionSpec] = {spec.name: spec for spec in _SPECS}


def register(spec: ActionSpec) -> None:
    if spec.name in ACTIONS and ACTIONS[spec.name] != spec:
        raise ValueError(f"Audit action {spec.name!r} is already registered differently")
    ACTIONS[spec.name] = spec
