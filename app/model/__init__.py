"""Model layer - the database schema.

SQLModel/SQLAlchemy table definitions for every persisted entity. Each model
inherits :class:`app.model.base_model.BaseModel`, which contributes the common
``id``, timezone-aware ``created_at``/``updated_at``, and the
``apiKey_used``/``oauth_user_id`` audit columns to every table. See the
:doc:`domain model </domain-model>` for the entity-relationship overview.
"""

import importlib
import pkgutil


def import_all_models() -> list[str]:
    """
    Import every module in this package so all tables register on
    ``SQLModel.metadata``.

    Alembic's ``env.py`` calls this before autogenerate/check. Importing the
    package's modules dynamically means a new model file is picked up without
    anyone remembering to add an import; a table missing from the metadata
    would otherwise make autogenerate emit a ``drop_table`` for it.

    Returns:
        list[str]: The imported module names, for logging and tests.
    """
    names = sorted(m.name for m in pkgutil.iter_modules(__path__))
    for name in names:
        importlib.import_module(f"{__name__}.{name}")
    return names
