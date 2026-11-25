"""The feature catalogue: regression/catalogue/*.yaml.

    - feature: "Invitations go only to the address on file"
      since: M1
      docs: docs/decisions.md#d17
      checks: [UNIT-123]                             # exact IDs (optional)
      match: ["^unit:accounts/tests/test_invitations"]  # regexes over "<stage>:<check>"

A check belongs to the first feature that lists its ID or whose `match` finds it.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def load_catalogue() -> list[dict]:
    out = []
    for path in sorted((HERE / "catalogue").glob("*.yaml")):
        for item in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            item["area"] = path.stem
            item["_re"] = [re.compile(m, re.I) for m in item.get("match") or []]
            out.append(item)
    return out


def claims(item: dict, stage: str, rid: str, check: str) -> bool:
    return rid in (item.get("checks") or []) or any(
        r.search(f"{stage}:{check}") for r in item["_re"]
    )


def feature_for(catalogue: list[dict], stage: str, rid: str, check: str) -> str:
    for item in catalogue:
        if claims(item, stage, rid, check):
            return item["feature"]
    return ""
