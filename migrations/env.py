import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlmodel import SQLModel

from app.core.config import configs
from app.core.database import _to_async_url
from app.model import import_all_models

# Register every table on SQLModel.metadata before autogenerate/check runs.
# This imports all of app/model dynamically: an explicit import list here
# drifted (ExportAuditLog and StrategyExecutionLog were missing), which made
# autogenerate propose dropping their tables.
import_all_models()

cmd_kwargs = context.get_x_argument(as_dictionary=True)
if "ENV" in cmd_kwargs:
    os.environ["ENV"] = cmd_kwargs["ENV"]
    ENV = cmd_kwargs["ENV"]
else:
    ENV = "test"


config = context.config
db_url = os.getenv("ALEMBIC_DATABASE_URL") or configs.DATABASE_URI
if not db_url:
    raise RuntimeError(
        "No DB URL found. Set ALEMBIC_DATABASE_URL or configs.DATABASE_URI"
    )

# "%%": the Alembic config is a ConfigParser, which treats "%" (as in a
# percent-encoded password) as interpolation syntax.
config.set_main_option("sqlalchemy.url", _to_async_url(db_url).replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = SQLModel.metadata


def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online():
    """Run migrations in 'online' mode against an ``AsyncEngine``."""

    db_config = config.get_section(config.config_ini_section) or {}
    db_config["sqlalchemy.url"] = config.get_main_option("sqlalchemy.url")

    connectable = async_engine_from_config(
        db_config,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    pass
else:
    asyncio.run(run_migrations_online())
