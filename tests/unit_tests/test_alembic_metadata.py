"""
Guards that keep ``alembic check`` clean.

``migrations/env.py`` registers tables through ``import_all_models()``. A
model missing from ``SQLModel.metadata`` makes autogenerate propose dropping
its table, which is how ``exportauditlog`` and ``strategyexecutionlog`` were
once at risk. The full model-vs-database comparison needs Postgres and runs
in the coverage workflow (``alembic upgrade head && alembic check``).
"""

import ast
import subprocess
import sys
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parents[2] / "app" / "model"


def _table_classes_in_source() -> set[str]:
    """Names of every ``class X(..., table=True)`` defined under app/model."""
    names = set()
    for path in MODEL_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ClassDef) and any(
                kw.arg == "table"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in node.keywords
            ):
                names.add(node.name)
    return names


def test_import_all_models_registers_every_table_model():
    # Fresh interpreter: other tests import models, which would hide a model
    # that import_all_models() misses.
    code = (
        "from sqlmodel import SQLModel\n"
        "from app.model import import_all_models\n"
        "import_all_models()\n"
        "print(','.join(sorted(m.class_.__name__ "
        "for m in SQLModel._sa_registry.mappers)))\n"
    )
    out = (
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            cwd=MODEL_DIR.parents[1],
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    registered = set(out.split(","))

    expected = _table_classes_in_source()
    assert expected, "no table models found; is MODEL_DIR right?"
    assert expected <= registered, expected - registered


def test_metadata_sorts_without_foreign_key_cycle_warning():
    """apikey <-> oauthusers reference each other; use_alter on one side must
    keep SQLAlchemy able to sort them, or it ignores those foreign keys."""
    import warnings

    from sqlmodel import SQLModel

    from app.model import import_all_models

    import_all_models()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        tables = [t.name for t in SQLModel.metadata.sorted_tables]

    assert tables.index("oauthusers") < tables.index("apikey")
