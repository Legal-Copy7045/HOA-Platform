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
]

ACTIONS: dict[str, ActionSpec] = {spec.name: spec for spec in _SPECS}


def register(spec: ActionSpec) -> None:
    if spec.name in ACTIONS and ACTIONS[spec.name] != spec:
        raise ValueError(f"Audit action {spec.name!r} is already registered differently")
    ACTIONS[spec.name] = spec
