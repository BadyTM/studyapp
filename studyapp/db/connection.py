"""Opening the database, with the three settings SQLite gets wrong by default.

Every connection needs all three. They are per-connection, not stored in the file,
so a connection opened anywhere else without them silently loses foreign keys.
"""

import sqlite3
from pathlib import Path

from .. import config


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """A connection with WAL, a busy timeout and foreign keys on.

    WAL lets the reader in the UI carry on while the generator writes. The busy
    timeout covers the moments they overlap anyway. Foreign keys are off by
    default in SQLite -- without this line the REFERENCES clauses are decoration.
    """
    connection = sqlite3.connect(str(path or config.db_path()))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def alembic_config(path: str | Path | None = None):
    """Alembic pointed at the app's database, wherever that lives."""
    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent.parent
    config_object = Config(str(root / "alembic.ini"))
    config_object.set_main_option("script_location", str(root / "studyapp" / "db" / "migrations"))
    database = Path(path or config.db_path()).as_posix()
    config_object.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config_object


def ensure_schema(path: str | Path | None = None) -> None:
    """Brings the database up to the newest migration, creating it if absent."""
    from alembic import command

    Path(path or config.db_path()).parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(path), "head")
