"""The database layer, on a throwaway file per test.

Two things are worth pinning down here rather than discovering during a
generation run: that the connection really does have foreign keys on, and that
regenerating a chunk leaves hand-edited questions alone.
"""

from pathlib import Path

import pytest

from studyapp.db import connection as db_connection
from studyapp.db import repo


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "test.db"
    db_connection.ensure_schema(path)
    connection = db_connection.connect(path)
    yield connection
    connection.close()


@pytest.fixture
def document(database):
    return repo.insert_document(database, "book.pdf", "book.pdf", "sha-1", 200, "cs")


@pytest.fixture
def two_books(database):
    """Two documents, one chunk each, two questions in the first and one in the second."""
    ids = []
    for name, title, count in (("a.pdf", "A", 2), ("b.pdf", "B", 1)):
        document_id = repo.insert_document(database, name, name, f"sha-{name}", 10, "cs")
        database.execute(
            """INSERT INTO chunks (document_id, ordinal, page_start, page_end, title,
                                   text, sha256, status, updated_at)
               VALUES (?, 0, 0, 5, ?, 't', ?, 'done', ?)""",
            (document_id, title, f"chunk-{name}", repo.utcnow()))
        database.commit()
        chunk_id = database.execute(
            "SELECT id FROM chunks WHERE document_id = ?", (document_id,)).fetchone()["id"]
        repo.insert_questions(database, chunk_id, [
            {"type": "open", "question": f"{title}{n}", "correct_text": "x",
             "source_quote": "q" * 40, "quote_score": 1.0, "page": 1}
            for n in range(count)])
        ids.append(document_id)
    return ids


class FakeChunk:
    def __init__(self, ordinal, page_start, page_end, title=None):
        self.ordinal = ordinal
        self.page_start = page_start
        self.page_end = page_end
        self.title = title
        self.text = f"text of chunk {ordinal}"
        self.sha256 = f"chunk-sha-{ordinal}"


def question(**overrides):
    base = {
        "type": "mcq",
        "question": "Ktera vlastnost transakce zarucuje trvalost zmen?",
        "options": ["Atomicita", "Konzistence", "Izolace", "Trvalost"],
        "correct_index": 3,
        "explanation": "Trvalost se tyka prezitli zmen po padu systemu.",
        "source_quote": "Trvalost znamena, ze po uspesnem potvrzeni transakce",
        "quote_score": 1.0,
        "page": 43,
        "rects": [[72.0, 100.0, 400.0, 114.0]],
    }
    base.update(overrides)
    return base


class TestConnection:
    def test_pragmas_are_set(self, database):
        assert database.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert database.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_foreign_keys_are_enforced(self, database):
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            repo.insert_questions(database, 999, [question()])


class TestDocuments:
    def test_round_trip(self, database, document):
        row = repo.get_document_by_sha(database, "sha-1")
        assert row["id"] == document
        assert row["language"] == "cs"
        assert row["page_count"] == 200

    def test_unknown_sha(self, database):
        assert repo.get_document_by_sha(database, "nope") is None

    def test_sha_is_unique(self, database, document):
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            repo.insert_document(database, "other.pdf", "other.pdf", "sha-1", 10)


class TestChunks:
    def test_inserted_chunks_start_pending(self, database, document):
        chunks = [FakeChunk(0, 0, 4, "1 Uvod"), FakeChunk(1, 5, 9, "2 Model")]
        assert repo.insert_chunks(database, document, chunks) == 2
        pending = repo.get_pending_chunks(database, document)
        assert len(pending) == 2
        assert all(row["status"] == "pending" for row in pending)
        assert pending[0]["title"] == "1 Uvod"

    def test_running_counts_as_pending(self, database, document):
        # A run killed mid-flight left the row 'running' and wrote nothing, so the
        # work still has to be picked up on the next start.
        repo.insert_chunks(database, document, [FakeChunk(0, 0, 4)])
        chunk = repo.get_pending_chunks(database, document)[0]
        repo.set_chunk_status(database, chunk["id"], "running")
        assert len(repo.get_pending_chunks(database, document)) == 1

    def test_done_chunks_are_not_pending(self, database, document):
        repo.insert_chunks(database, document, [FakeChunk(0, 0, 4)])
        chunk = repo.get_pending_chunks(database, document)[0]
        repo.set_chunk_status(database, chunk["id"], "done")
        assert repo.get_pending_chunks(database, document) == []

    def test_status_counts(self, database, document):
        repo.insert_chunks(database, document, [FakeChunk(n, n, n) for n in range(3)])
        rows = repo.get_pending_chunks(database, document)
        repo.set_chunk_status(database, rows[0]["id"], "done")
        repo.set_chunk_status(database, rows[1]["id"], "skipped")
        assert repo.chunk_status_counts(database, document) == {
            "done": 1, "skipped": 1, "pending": 1,
        }

    def test_failure_keeps_its_message(self, database, document):
        repo.insert_chunks(database, document, [FakeChunk(0, 0, 4)])
        chunk = repo.get_pending_chunks(database, document)[0]
        repo.set_chunk_status(database, chunk["id"], "failed", "HTTP 529")
        assert repo.get_chunk(database, chunk["id"])["error"] == "HTTP 529"

    def test_reset_clears_a_previous_error(self, database, document):
        repo.insert_chunks(database, document, [FakeChunk(0, 0, 4)])
        chunk = repo.get_pending_chunks(database, document)[0]
        repo.set_chunk_status(database, chunk["id"], "failed", "HTTP 529")
        repo.reset_chunks(database, [chunk["id"]])
        row = repo.get_chunk(database, chunk["id"])
        assert row["status"] == "pending" and row["error"] is None


class TestQuestions:
    @pytest.fixture
    def chunk(self, database, document):
        repo.insert_chunks(database, document, [FakeChunk(0, 0, 4, "6 Transakce")])
        return repo.get_pending_chunks(database, document)[0]["id"]

    def test_options_survive_the_json_round_trip(self, database, chunk):
        import json

        repo.insert_questions(database, chunk, [question()])
        row = database.execute("SELECT * FROM questions").fetchone()
        assert json.loads(row["options_json"])[row["correct_index"]] == "Trvalost"

    def test_open_questions_have_no_options(self, database, chunk):
        repo.insert_questions(database, chunk, [question(
            type="open", options=None, correct_index=None,
            correct_text="Trvalost zarucuje prezitli zmen.",
        )])
        row = database.execute("SELECT * FROM questions").fetchone()
        assert row["options_json"] is None
        assert row["correct_text"].startswith("Trvalost")

    def test_questions_are_active_by_default(self, database, chunk):
        repo.insert_questions(database, chunk, [question()])
        assert repo.count_questions(database) == 1

    def test_regeneration_retires_generated_questions(self, database, chunk):
        repo.insert_questions(database, chunk, [question(), question()])
        assert repo.deactivate_unedited_questions(database, chunk) == 2
        assert repo.count_questions(database, active_only=True) == 0
        assert repo.count_questions(database, active_only=False) == 2

    def test_regeneration_leaves_hand_edited_questions_alone(self, database, chunk):
        repo.insert_questions(database, chunk, [question(), question()])
        database.execute(
            "UPDATE questions SET edited_at = ? WHERE id = (SELECT MIN(id) FROM questions)",
            (repo.utcnow(),),
        )
        database.commit()
        assert repo.deactivate_unedited_questions(database, chunk) == 1
        assert repo.count_questions(database, active_only=True) == 1

    def test_needs_review_catches_a_missing_page(self, database, chunk):
        repo.insert_questions(database, chunk, [question(page=None)])
        assert len(repo.search_questions(database, state="review")) == 1

    def test_needs_review_catches_a_weak_quote_match(self, database, chunk):
        repo.insert_questions(database, chunk, [question(quote_score=0.91)])
        assert len(repo.search_questions(database, state="review")) == 1

    def test_a_clean_question_needs_no_review(self, database, chunk):
        repo.insert_questions(database, chunk, [question()])
        assert repo.search_questions(database, state="review") == []

    def test_question_counts_appear_beside_their_chunk(self, database, document, chunk):
        repo.insert_questions(database, chunk, [question(), question()])
        row = repo.list_chunks(database, document)[0]
        assert row["question_count"] == 2
        assert row["title"] == "6 Transakce"


def test_timestamps_are_utc_iso8601():
    stamp = repo.utcnow()
    from datetime import datetime, timezone

    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


class TestFilteringByDocument:
    """Two books in one bank. Nothing about one may leak into a view of the other."""

    def test_search_without_a_document_sees_everything(self, database, two_books):
        assert len(repo.search_questions(database)) == 3

    def test_search_within_one_document(self, database, two_books):
        first, second = two_books
        assert len(repo.search_questions(database, document_id=first)) == 2
        assert len(repo.search_questions(database, document_id=second)) == 1

    def test_counting_within_one_document(self, database, two_books):
        first, second = two_books
        assert repo.count_questions(database) == 3
        assert repo.count_questions(database, document_id=first) == 2
        assert repo.count_questions(database, document_id=second) == 1

    def test_chapters_within_one_document(self, database, two_books):
        first, second = two_books
        assert [row["title"] for row in repo.list_chapters(database, first)] == ["A"]
        assert [row["title"] for row in repo.list_chapters(database, second)] == ["B"]
        # No document means every chapter, for a bank-wide view.
        assert {row["title"] for row in repo.list_chapters(database)} == {"A", "B"}

    def test_an_unknown_document_matches_nothing(self, database, two_books):
        assert repo.search_questions(database, document_id=9999) == []
        assert repo.count_questions(database, document_id=9999) == 0

    def test_the_mistakes_queue_can_be_scoped_too(self, database, two_books):
        first, second = two_books
        wrong = repo.search_questions(database, document_id=second)[0]
        repo.record_attempt(database, None, wrong["id"], is_correct=False)
        assert repo.mistakes_count(database) == 1
        assert repo.mistakes_count(database, document_id=first) == 0
        assert repo.mistakes_count(database, document_id=second) == 1


class TestARenamedPDF:
    """Someone renames the file. The bank must survive it, and say so clearly.

    The path is absolute and recorded at import; the hash is what actually
    identifies the book. Everything else -- pages, quotes, rectangles, the
    blurred page cache -- is keyed on content and does not care what the file is
    called.
    """

    @pytest.fixture
    def book(self, tmp_path):
        import pymupdf

        path = tmp_path / "skripta.pdf"
        document = pymupdf.open()
        document.new_page().insert_text((72, 300), "Trvalost", fontsize=12,
                                        fontname="helv")
        document.save(path)
        document.close()
        return path

    @pytest.fixture
    def imported(self, database, book):
        from studyapp.ingest.extract import file_sha256

        return repo.insert_document(database, str(book), book.name,
                                    file_sha256(book), 1, "cs")

    def test_a_renamed_file_reads_as_missing(self, database, imported, book):
        book.rename(book.with_name("jiny nazev.pdf"))
        assert not repo.document_is_present(database, imported)

    def test_finding_it_again_takes_the_new_name_too(self, database, imported, book):
        # The picker went on showing the old filename, and the "Find ..." dialog
        # sent the reader looking for a file that no longer exists under it.
        renamed = book.with_name("Fyziologie 2026 - finalni.pdf")
        book.rename(renamed)

        assert repo.relocate_document(database, imported, renamed)
        row = repo.get_document(database, imported)
        assert row["filename"] == "Fyziologie 2026 - finalni.pdf"
        assert Path(row["path"]).name == "Fyziologie 2026 - finalni.pdf"
        assert repo.document_is_present(database, imported)

    def test_the_questions_are_untouched_by_the_rename(self, database, imported, book):
        database.execute(
            """INSERT INTO chunks (document_id, ordinal, page_start, page_end,
                                   title, text, sha256, status, updated_at)
               VALUES (?, 0, 0, 1, 'K', 't', 'c1', 'done', ?)""",
            (imported, repo.utcnow()))
        database.commit()
        chunk_id = database.execute("SELECT id FROM chunks").fetchone()["id"]
        repo.insert_questions(database, chunk_id, [{
            "type": "open", "question": "Co?", "correct_text": "To.",
            "source_quote": "x" * 40, "quote_score": 1.0, "page": 0,
            "rects": [[1.0, 2.0, 3.0, 4.0]]}])
        before = [tuple(r) for r in database.execute(
            "SELECT page, rects_json, source_quote FROM questions")]

        renamed = book.with_name("uplne jinak.pdf")
        book.rename(renamed)
        repo.relocate_document(database, imported, renamed)

        after = [tuple(r) for r in database.execute(
            "SELECT page, rects_json, source_quote FROM questions")]
        assert after == before

    def test_a_different_file_under_the_right_name_is_still_refused(
            self, database, imported, book, tmp_path):
        # A new edition would keep every stored page number while the text under
        # it had moved. The name is not what is checked.
        import pymupdf

        impostor = tmp_path / "skripta.pdf.bak"
        other = pymupdf.open()
        other.new_page().insert_text((72, 300), "Jiné vydání", fontsize=12,
                                     fontname="helv")
        other.save(impostor)
        other.close()

        assert not repo.relocate_document(database, imported, impostor)
        assert repo.get_document(database, imported)["filename"] == "skripta.pdf"


class TestAnImportIsAllOrNothing:
    """`documents.sha256` is unique, so a half-written import is permanent.

    The document used to be committed before its chunks. A failure in between
    left a row with nothing behind it, and from then on that file answered
    "already imported, nothing to do" with nothing to generate from.
    """

    def test_a_failure_writing_chunks_leaves_no_document(self, database, tmp_path):
        from studyapp.ingest import importer

        class Prepared:
            path = tmp_path / "book.pdf"
            sha256 = "sha-rollback"
            page_count = 10
            language = "cs"
            chunks = [object()]          # no attributes: insert_chunks will fail

        with pytest.raises(Exception):
            importer.store(database, Prepared())

        assert repo.get_document_by_sha(database, "sha-rollback") is None

    def test_the_same_file_can_be_imported_again_afterwards(self, database, tmp_path):
        from studyapp.ingest import importer

        class Broken:
            path = tmp_path / "book.pdf"
            sha256 = "sha-retry"
            page_count = 10
            language = "cs"
            chunks = [object()]

        class Good(Broken):
            chunks = [type("C", (), {
                "ordinal": 0, "page_start": 0, "page_end": 5, "title": "K",
                "text": "t", "sha256": "c1"})()]

        with pytest.raises(Exception):
            importer.store(database, Broken())
        document_id, is_new = importer.store(database, Good())

        assert is_new, "the failed attempt blocked the real one"
        assert len(repo.all_chunks(database, document_id)) == 1

    def test_a_normal_import_still_commits(self, database, tmp_path):
        from studyapp.ingest import importer

        class Prepared:
            path = tmp_path / "book.pdf"
            sha256 = "sha-ok"
            page_count = 10
            language = "cs"
            chunks = [type("C", (), {
                "ordinal": 0, "page_start": 0, "page_end": 5, "title": "K",
                "text": "t", "sha256": "c1"})()]

        document_id, is_new = importer.store(database, Prepared())
        assert is_new and repo.get_document(database, document_id) is not None
