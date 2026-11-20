"""Decision logic of migration e6b3c8d1f4a2 (userpoints user/time index)."""

import importlib.util
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations/versions/e6b3c8d1f4a2_userpoints_user_created_index.py"
)


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("mig_e6b3c8d1f4a2", PATH)
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
    seq = iter(states)
    monkeypatch.setattr(module, "_index_is_valid", lambda name: next(seq))


def test_upgrade_builds_covering_index_concurrently(migration, monkeypatch):
    _validity(migration, monkeypatch, [None, True])

    migration.upgrade()

    assert migration.calls == [("create", migration.NAME)]
    args, kwargs = migration.op.create_index.call_args
    assert args[2] == ["userId", "created_at"]
    assert kwargs["postgresql_include"] == ["taskId", "points"]
    assert kwargs["postgresql_concurrently"] is True


def test_upgrade_rebuilds_invalid_leftover(migration, monkeypatch):
    _validity(migration, monkeypatch, [False, True])

    migration.upgrade()

    assert migration.calls == [("drop", migration.NAME), ("create", migration.NAME)]


def test_upgrade_fails_loudly_when_index_ends_invalid(migration, monkeypatch):
    _validity(migration, monkeypatch, [None, False])

    with pytest.raises(RuntimeError, match="ix_userpoints_user_created"):
        migration.upgrade()


def test_downgrade_drops_index(migration):
    migration.downgrade()

    assert migration.calls == [("drop", migration.NAME)]
