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
]

ACTIONS: dict[str, ActionSpec] = {spec.name: spec for spec in _SPECS}


def register(spec: ActionSpec) -> None:
    if spec.name in ACTIONS and ACTIONS[spec.name] != spec:
        raise ValueError(f"Audit action {spec.name!r} is already registered differently")
    ACTIONS[spec.name] = spec
