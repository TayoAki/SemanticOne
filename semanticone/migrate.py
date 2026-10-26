"""Run Alembic migrations programmatically (used at startup and by the CLI)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine

_LOCK_ID = 5_810_243_771  # arbitrary constant shared by every SemanticOne process

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(engine: Engine | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if engine is not None:
        config.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%"))
    return config


def upgrade_database(engine: Engine, revision: str = "head") -> None:
    config = alembic_config(engine)
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            # Several workers may start at once; only one migrates at a time.
            connection.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _LOCK_ID})
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
