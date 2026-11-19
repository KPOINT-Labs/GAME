"""
Decision logic of migration b8d2e6f4a1c7 (covering index on userpoints).

A failed ``CREATE INDEX CONCURRENTLY`` leaves an INVALID index under the new
name; ``IF NOT EXISTS`` would skip it on retry and the old index would then be
dropped, leaving no usable index. The real Postgres behaviour was reproduced
by hand; these tests pin the decisions with Alembic's ``op`` mocked.
"""

import importlib.util
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "b8d2e6f4a1c7_userpoints_covering_leaderboard_index.py"
)


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("mig_b8d2e6f4a1c7", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    op = MagicMock()
    op.get_context.return_value.autocommit_block.return_value = nullcontext()
    op.create_index.side_effect = lambda name, *a, **k: calls.append(("create", name))
    op.drop_index.side_effect = lambda name, *a, **k: calls.append(("drop", name))
    monkeypatch.setattr(module, "op", op)
    monkeypatch.setattr(module.context, "is_offline_mode", lambda: False)
    module.calls = calls
    return module


def _validity(module, monkeypatch, states):
    """Feed successive ``_index_is_valid`` results (before/after the build)."""
    seq = iter(states)
    monkeypatch.setattr(module, "_index_is_valid", lambda name: next(seq))


def test_upgrade_builds_then_drops_old(migration, monkeypatch):
    _validity(migration, monkeypatch, [None, True])

    migration.upgrade()

    assert migration.calls == [("create", migration.NEW), ("drop", migration.OLD)]
    kwargs = migration.op.create_index.call_args.kwargs
    assert kwargs["postgresql_include"] == ["points"]
    assert kwargs["postgresql_concurrently"] is True


def test_upgrade_rebuilds_invalid_leftover_before_dropping_old(migration, monkeypatch):
    _validity(migration, monkeypatch, [False, True])

    migration.upgrade()

    assert migration.calls == [
        ("drop", migration.NEW),
        ("create", migration.NEW),
        ("drop", migration.OLD),
    ]


def test_upgrade_keeps_old_index_when_new_one_is_not_valid(migration, monkeypatch):
    _validity(migration, monkeypatch, [None, False])

    with pytest.raises(RuntimeError, match="keeping ix_user_points_task_user_created"):
        migration.upgrade()

    assert ("drop", migration.OLD) not in migration.calls


def test_downgrade_mirrors_upgrade(migration, monkeypatch):
    _validity(migration, monkeypatch, [False, True])

    migration.downgrade()

    assert migration.calls == [
        ("drop", migration.OLD),
        ("create", migration.OLD),
        ("drop", migration.NEW),
    ]
    assert "postgresql_include" not in migration.op.create_index.call_args.kwargs
