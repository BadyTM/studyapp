"""Opening the database, with the three settings SQLite gets wrong by default.

Every connection needs all three. They are per-connection, not stored in the file,
so a connection opened anywhere else without them silently loses foreign keys.
"""

import re
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


MIGRATIONS = Path(__file__).resolve().parent / "migrations" / "versions"
_REVISION = re.compile(r"^revision\s*=\s*[\"']([^\"']+)", re.MULTILINE)
_DOWN = re.compile(r"^down_revision\s*=\s*[\"']([^\"']+)", re.MULTILINE)


def head_revision() -> str | None:
    """The newest migration, read off the files rather than from Alembic.

    The head is the revision nothing else names as its parent. Read this way it
    stays a single source of truth -- writing the id into the code as well would
    be the sort of constant that is right until the day it is not.
    """
    revisions, parents = [], set()
    for script in MIGRATIONS.glob("*.py"):
        text = script.read_text(encoding="utf-8")
        found = _REVISION.search(text)
        if not found:
            continue
        revisions.append(found.group(1))
        parent = _DOWN.search(text)
        if parent:
            parents.add(parent.group(1))
    heads = [revision for revision in revisions if revision not in parents]
    return heads[0] if len(heads) == 1 else None


def schema_is_current(path: str | Path | None = None) -> bool:
    """Whether the database is already at the newest migration.

    Asked with `sqlite3` on purpose. The answer is yes on every start after the
    first, and finding that out through Alembic means importing Alembic and
    SQLAlchemy to do nothing: measured at 423ms against 5ms for the check
    itself, on a window that takes 900ms to open.
    """
    database = Path(path or config.db_path())
    if not database.exists():
        return False
    head = head_revision()
    if head is None:
        return False            # more than one head, or none: let Alembic decide
    # Closed by hand: `sqlite3.connect` as a context manager commits or rolls
    # back and leaves the connection open, which would mean a read handle on
    # the database for the life of the process -- on Windows that is enough to
    # stop the file being replaced or moved.
    connection = None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        row = connection.execute(
            "SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error:
        return False            # no such table, locked, not a database at all
    finally:
        if connection is not None:
            connection.close()
    return bool(row) and row[0] == head


def ensure_schema(path: str | Path | None = None) -> None:
    """Brings the database up to the newest migration, creating it if absent."""
    if schema_is_current(path):
        return

    from alembic import command

    Path(path or config.db_path()).parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(path), "head")
