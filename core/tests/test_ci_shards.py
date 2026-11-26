"""CI runs the suite in parallel shards (`pytest --shard K/N`, conftest.py):
every test runs in exactly one shard."""

import pytest

from conftest import parse_shard, shard_items


def test_every_item_lands_in_exactly_one_shard():
    items = list(range(103))
    for n in (1, 3, 4):
        shards = [shard_items(items, k, n) for k in range(n)]
        assert sorted(x for s in shards for x in s) == items
        assert max(map(len, shards)) - min(map(len, shards)) <= 1


def test_shard_option_is_checked():
    assert parse_shard("") is None and parse_shard("2/4") == (2, 4)
    for bad in ("4/4", "x/4", "1", "-1/4"):
        with pytest.raises(pytest.UsageError):
            parse_shard(bad)


def test_ci_runs_every_shard():
    from pathlib import Path

    import yaml

    ci = yaml.safe_load(Path(".github/workflows/ci.yml").read_text())
    shards = ci["jobs"]["test-shard"]["strategy"]["matrix"]["shard"]
    run = " ".join(s.get("run", "") for s in ci["jobs"]["test-shard"]["steps"])
    assert "--shard ${{ matrix.shard }}/" + str(len(shards)) in run
    assert sorted(shards) == list(range(len(shards)))
    # The summary job keeps the "test" check name and fails if any shard failed.
    assert ci["jobs"]["test"]["needs"] == ["test-shard"]
