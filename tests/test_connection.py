"""Opening the database, and the shortcut around Alembic.

`ensure_schema` runs on every start and almost always has nothing to do, so it
answers "is this database current?" with sqlite3 rather than by importing
Alembic and SQLAlchemy to find out. That shortcut is only safe while it is
exactly as cautious as Alembic would be, which is what these check: every
uncertain answer has to be "no", because "no" merely costs the import.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from studyapp.db import connection as db_connection
from studyapp.db import repo

ROOT = Path(__file__).resolve().parent.parent


class TestFindingTheNewestMigration:
    def test_the_head_is_the_one_nothing_points_at(self):
        versions = sorted(p.name for p in db_connection.MIGRATIONS.glob("*.py"))
        assert db_connection.head_revision() is not None
        assert versions, "no migrations at all"

    def test_it_matches_what_alembic_would_say(self, tmp_path):
        """The head read off the files is the head Alembic stamps into the row."""
        from alembic import command

        path = tmp_path / "stamped.db"
        command.upgrade(db_connection.alembic_config(path), "head")

        connection = sqlite3.connect(path)
        try:
            stamped = connection.execute(
                "SELECT version_num FROM alembic_version").fetchone()[0]
        finally:
            connection.close()

        assert stamped == db_connection.head_revision()


class TestIsTheSchemaCurrent:
    def test_a_database_that_is_not_there_is_not_current(self, tmp_path):
        assert not db_connection.schema_is_current(tmp_path / "nothing.db")

    def test_a_fresh_one_is(self, tmp_path):
        path = tmp_path / "fresh.db"
        db_connection.ensure_schema(path)
        assert db_connection.schema_is_current(path)

    def test_a_file_that_is_not_a_database_is_not(self, tmp_path):
        path = tmp_path / "junk.db"
        path.write_bytes(b"this is not a database" * 50)
        assert not db_connection.schema_is_current(path)

    def test_an_empty_file_is_not(self, tmp_path):
        path = tmp_path / "empty.db"
        path.touch()
        assert not db_connection.schema_is_current(path)

    def _stamp(self, path, revision):
        connection = sqlite3.connect(path)
        try:
            connection.execute("UPDATE alembic_version SET version_num = ?",
                               (revision,))
            connection.commit()
        finally:
            connection.close()

    def test_a_database_from_another_revision_is_not(self, tmp_path):
        path = tmp_path / "old.db"
        db_connection.ensure_schema(path)
        self._stamp(path, "0000")
        assert not db_connection.schema_is_current(path)

    def test_and_ensure_schema_hands_it_to_alembic(self, tmp_path):
        """What a stale stamp must not do is go unnoticed.

        There is only one migration, so there is no older revision to put in the
        row and no upgrade to watch happen. `0000` stands in for one, and
        Alembic saying it cannot find that revision is the proof wanted here:
        the shortcut stepped aside instead of reporting the database current.
        """
        import alembic.util.exc

        path = tmp_path / "old.db"
        db_connection.ensure_schema(path)
        self._stamp(path, "0000")

        with pytest.raises(alembic.util.exc.CommandError):
            db_connection.ensure_schema(path)

    def test_a_database_with_no_version_table_is_not(self, tmp_path):
        path = tmp_path / "bare.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE something (id INTEGER)")
            connection.commit()
        finally:
            connection.close()
        assert not db_connection.schema_is_current(path)


class TestTheShortcutIsTakenAtAll:
    """The point of the exercise: a start that does not import Alembic.

    Run in a subprocess, because by now this one has imported it several times
    over and `sys.modules` would say so whatever the code did.
    """

    def test_a_current_database_never_reaches_alembic(self, tmp_path):
        path = tmp_path / "current.db"
        db_connection.ensure_schema(path)

        result = subprocess.run(
            [sys.executable, "-c",
             "import sys\n"
             f"sys.path.insert(0, {str(ROOT)!r})\n"
             "from studyapp.db import connection\n"
             f"connection.ensure_schema({str(path)!r})\n"
             "print('alembic' in sys.modules, 'sqlalchemy' in sys.modules)"],
            capture_output=True, text=True, timeout=120)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False False", result.stdout

    def test_a_missing_database_does_reach_it(self, tmp_path):
        path = tmp_path / "missing.db"
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys\n"
             f"sys.path.insert(0, {str(ROOT)!r})\n"
             "from studyapp.db import connection\n"
             f"connection.ensure_schema({str(path)!r})\n"
             "print('alembic' in sys.modules)"],
            capture_output=True, text=True, timeout=120)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "True"
        assert path.exists()


class TestTheConnectionItself:
    def test_the_three_pragmas_are_set(self, tmp_path):
        path = tmp_path / "pragmas.db"
        db_connection.ensure_schema(path)
        connection = db_connection.connect(path)
        try:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        finally:
            connection.close()

    def test_the_schema_check_lets_go_of_the_file(self, tmp_path):
        """The check opens the database read-only; it has to close it too.

        `sqlite3.connect` used as a context manager commits or rolls back and
        leaves the connection open, which is what this was written with -- and
        on Windows one open handle is enough to stop the file being moved or
        replaced. Writing a row afterwards does not catch it, because a reader
        does not block a writer; moving the file does.
        """
        path = tmp_path / "unlocked.db"
        db_connection.ensure_schema(path)
        assert db_connection.schema_is_current(path)

        os.replace(path, tmp_path / "moved.db")     # raises if anything holds it
        assert (tmp_path / "moved.db").exists()

    def test_a_write_still_works_after_it(self, tmp_path):
        path = tmp_path / "writable.db"
        db_connection.ensure_schema(path)
        db_connection.schema_is_current(path)

        connection = db_connection.connect(path)
        try:
            repo.insert_document(connection, "a.pdf", "a.pdf", "sha", 1, "cs")
            assert repo.list_documents(connection)
        finally:
            connection.close()
