"""regression/ids/*.yaml: every line is one the ID tool (regression/run.py Ids) reads.

A line the tool's pattern does not match (for example a two-digit "MAN-54")
is silently ignored by the loader, so its check would be issued a second
number on the next run. No database needed.
"""

from __future__ import annotations

import json
import re

import pytest
import yaml

from regression import run

FILES = sorted(run.IDS.glob("*.yaml"))


def test_there_is_an_ids_file_per_stage():
    assert {path.stem for path in FILES} >= set(run.PREFIX)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_line_matches_the_tool_pattern(path):
    prefix = run.PREFIX[path.stem]
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == [
        f"# Stable check IDs for the {path.stem} stage (regression/run.py). Append-only: never",
        "# renumber or reuse an ID. Renamed a check? Edit its key here to keep its ID.",
    ]
    high = run._HIGH.match(lines[2])
    assert high, f"{path.name}: line 3 must be '# highest issued: {prefix}-NNN'"
    assert lines[2] == f"# highest issued: {prefix}-{int(high.group(1)):03d}"
    numbers, keys = [], []
    for number, line in enumerate(lines[3:], start=4):
        match = run._LINE.match(line)
        assert match, f"{path.name}:{number} is not read by the ID tool: {line!r}"
        rid = match.group(1)
        assert rid.startswith(prefix + "-"), f"{path.name}:{number}: wrong prefix {rid}"
        assert rid == f"{prefix}-{int(rid.split('-')[1]):03d}", f"{path.name}:{number}: {rid}"
        numbers.append(int(rid.split("-")[1]))
        keys.append(json.loads(match.group(2)))
    assert len(numbers) == len(set(numbers)), f"{path.name}: a number is issued twice"
    assert len(keys) == len(set(keys)), f"{path.name}: a check has two numbers"
    assert not numbers or int(high.group(1)) >= max(numbers)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_loader_reads_every_line(path):
    entries = sum(1 for line in path.read_text(encoding="utf-8").splitlines()[3:] if line)
    ids = run.Ids(path.stem)
    assert len(ids.by_key) == entries


def test_every_manual_check_has_an_id():
    checks = yaml.safe_load((run.HERE / "manual.yaml").read_text(encoding="utf-8"))
    ids = run.Ids("manual")
    missing = [check["key"] for check in checks if check["key"] not in ids.by_key]
    assert not missing, f"issue IDs for these manual checks: {missing}"


def test_new_ids_are_zero_padded(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "IDS", tmp_path)
    (tmp_path / "manual.yaml").write_text(
        '# highest issued: MAN-009\nMAN-009: "old"\n', encoding="utf-8"
    )
    ids = run.Ids("manual")
    assert ids.get("old") == "MAN-009"
    assert ids.get("new") == "MAN-010"
    ids.save()
    assert re.search(r"^MAN-010: \"new\"$", (tmp_path / "manual.yaml").read_text(), re.M)
