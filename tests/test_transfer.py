"""Moving a bank between databases.

The property worth pinning down is that an export is a faithful copy and an
import is additive. Both halves have a way of being quietly wrong: an export that
drops a field looks fine until someone practises from it, and an import that
matches on the wrong thing either duplicates the whole bank or silently refuses
to add anything.
"""

import json

import pytest

from studyapp.db import connection as db_connection
from studyapp.db import repo, transfer


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "source.db"
    db_connection.ensure_schema(path)
    connection = db_connection.connect(path)
    yield connection
    connection.close()


@pytest.fixture
def empty(tmp_path):
    path = tmp_path / "target.db"
    db_connection.ensure_schema(path)
    connection = db_connection.connect(path)
    yield connection
    connection.close()


def question(**overrides):
    base = {
        "type": "mcq",
        "question": "Která vlastnost zaručuje trvalost?",
        "options": ["Atomicita", "Konzistence", "Izolace", "Trvalost"],
        "correct_index": 3,
        "explanation": "Trvalost.",
        "source_quote": "Trvalost znamená, že změny zůstávají zachovány.",
        "quote_score": 1.0,
        "page": 42,
        "rects": [[72.0, 100.0, 400.0, 114.0]],
    }
    base.update(overrides)
    return base


@pytest.fixture
def bank(database):
    """One document, two chunks, three questions -- one of them hand-edited."""
    document_id = repo.insert_document(
        database, "/somewhere/book.pdf", "book.pdf", "sha-book", 200, "cs")
    for ordinal, title in ((0, "1 Úvod"), (1, "6 Transakce")):
        database.execute(
            """INSERT INTO chunks (document_id, ordinal, page_start, page_end,
                                   title, text, sha256, status, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'done', ?)""",
            (document_id, ordinal, ordinal * 40, ordinal * 40 + 5, title,
             f"passage {ordinal}", f"chunk-sha-{ordinal}", repo.utcnow()))
    database.commit()
    chunks = [row["id"] for row in
              database.execute("SELECT id FROM chunks ORDER BY ordinal")]
    repo.insert_questions(database, chunks[0], [question(), question(
        type="open", question="Co je izolace?", options=None,
        correct_index=None, correct_text="Oddělení transakcí.")])
    repo.insert_questions(database, chunks[1], [question(question="A třetí?")])
    return document_id


def roundtrip(database, target, tmp_path, document_id=None):
    path = tmp_path / "export.json"
    transfer.write(database, path, document_id)
    return transfer.apply(target, transfer.read(path))


def rows(connection):
    return [tuple(r) for r in connection.execute("""
        SELECT q.type, q.question, q.options_json, q.correct_index, q.correct_text,
               q.explanation, q.source_quote, q.quote_score, q.page, q.rects_json,
               q.is_active, q.starred, q.edited_at, c.title, c.ordinal, c.text
        FROM questions q JOIN chunks c ON c.id = q.chunk_id
        ORDER BY c.ordinal, q.question""")]


class TestAnExportIsAFaithfulCopy:
    def test_every_question_survives_the_trip(self, database, empty, bank, tmp_path):
        roundtrip(database, empty, tmp_path)
        assert rows(empty) == rows(database)

    def test_the_hand_edited_mark_travels(self, database, empty, bank, tmp_path):
        # It is what protects a correction from the next regeneration; losing it
        # would put the correction quietly back in the firing line.
        first = repo.search_questions(database)[0]
        repo.update_question(database, first["id"], question="Opraveno ručně")
        roundtrip(database, empty, tmp_path)
        edited = [r for r in repo.search_questions(empty)
                  if r["question"] == "Opraveno ručně"]
        assert edited and edited[0]["edited_at"]

    def test_stars_and_retirement_travel(self, database, empty, bank, tmp_path):
        ids = [row["id"] for row in repo.search_questions(database)]
        repo.set_starred(database, ids[0], True)
        repo.set_active(database, [ids[1]], False)
        roundtrip(database, empty, tmp_path)
        assert sum(r["starred"] for r in repo.search_questions(empty, state="all")) == 1
        assert len(repo.search_questions(empty, state="inactive")) == 1

    def test_practice_does_not_travel(self, database, empty, bank, tmp_path):
        # The bank is shareable; what someone answered is theirs.
        first = repo.search_questions(database)[0]
        repo.record_attempt(database, None, first["id"], is_correct=False)
        roundtrip(database, empty, tmp_path)
        assert repo.overall_accuracy(empty) == (0, 0)
        assert repo.mistakes_count(empty) == 0

    def test_embeddings_do_not_travel(self, database, empty, bank, tmp_path):
        # A cache, and a large one; dedupe.py can rebuild it.
        first = repo.search_questions(database)[0]
        repo.store_embeddings(database, [(first["id"], b"\x00" * 16)])
        roundtrip(database, empty, tmp_path)
        assert all(r["embedding"] is None
                   for r in empty.execute("SELECT embedding FROM questions"))

    def test_the_passage_travels_so_the_quote_can_still_be_checked(
            self, database, empty, bank, tmp_path):
        roundtrip(database, empty, tmp_path)
        texts = [r["text"] for r in empty.execute("SELECT text FROM chunks")]
        assert sorted(texts) == ["passage 0", "passage 1"]

    def test_a_chunk_with_no_questions_is_left_behind(self, database, empty,
                                                      bank, tmp_path):
        database.execute(
            """INSERT INTO chunks (document_id, ordinal, page_start, page_end,
                                   title, text, sha256, status, updated_at)
               VALUES (?, 9, 90, 95, 'Prázdná', 'nic', 'chunk-sha-9', 'skipped', ?)""",
            (bank, repo.utcnow()))
        database.commit()
        roundtrip(database, empty, tmp_path)
        assert empty.execute(
            "SELECT COUNT(*) FROM chunks WHERE sha256 = 'chunk-sha-9'"
        ).fetchone()[0] == 0

    def test_only_the_chosen_document_is_exported(self, database, empty,
                                                  bank, tmp_path):
        other = repo.insert_document(database, "/b.pdf", "b.pdf", "sha-b", 10, "cs")
        database.execute(
            """INSERT INTO chunks (document_id, ordinal, page_start, page_end,
                                   title, text, sha256, status, updated_at)
               VALUES (?, 0, 0, 5, 'Jiná', 'text', 'chunk-sha-b', 'done', ?)""",
            (other, repo.utcnow()))
        database.commit()
        chunk_id = database.execute(
            "SELECT id FROM chunks WHERE sha256 = 'chunk-sha-b'").fetchone()["id"]
        repo.insert_questions(database, chunk_id, [question(question="Z druhé knihy")])

        roundtrip(database, empty, tmp_path, document_id=bank)
        assert len(repo.list_documents(empty)) == 1
        assert not [r for r in repo.search_questions(empty)
                    if r["question"] == "Z druhé knihy"]


class TestAnImportIsAdditive:
    def test_importing_twice_changes_nothing_the_second_time(
            self, database, empty, bank, tmp_path):
        # The obvious way to find out whether an import worked is to do it again.
        path = tmp_path / "export.json"
        transfer.write(database, path, None)
        payload = transfer.read(path)

        first = transfer.apply(empty, payload)
        second = transfer.apply(empty, payload)

        assert first.questions_added == 3
        assert second.questions_added == 0
        assert second.questions_skipped == 3
        assert repo.count_questions(empty, active_only=False) == 3

    def test_a_document_already_here_is_matched_by_its_pdf_not_its_name(
            self, database, empty, bank, tmp_path):
        # Filenames get renamed; the hash is what says it is the same book.
        repo.insert_document(empty, "/elsewhere/renamed.pdf", "renamed.pdf",
                             "sha-book", 200, "cs")
        report = roundtrip(database, empty, tmp_path)
        assert report.documents_added == 0
        assert report.documents_matched == 1
        assert len(repo.list_documents(empty)) == 1

    def test_new_questions_land_in_a_chunk_that_is_already_here(
            self, database, empty, bank, tmp_path):
        roundtrip(database, empty, tmp_path)
        chunks_before = empty.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

        extra = repo.search_questions(database)[0]["chunk_id"]
        repo.insert_questions(database, extra, [question(question="Pozdější otázka")])
        report = roundtrip(database, empty, tmp_path)

        assert report.questions_added == 1
        assert report.chunks_added == 0
        assert empty.execute(
            "SELECT COUNT(*) FROM chunks").fetchone()[0] == chunks_before

    def test_nothing_in_the_bank_is_deleted_or_changed(self, database, empty,
                                                       bank, tmp_path):
        mine = repo.insert_document(empty, "/mine.pdf", "mine.pdf", "sha-mine", 5, "cs")
        empty.execute(
            """INSERT INTO chunks (document_id, ordinal, page_start, page_end,
                                   title, text, sha256, status, updated_at)
               VALUES (?, 0, 0, 1, 'Moje', 'text', 'chunk-mine', 'done', ?)""",
            (mine, repo.utcnow()))
        empty.commit()
        chunk_id = empty.execute(
            "SELECT id FROM chunks WHERE sha256 = 'chunk-mine'").fetchone()["id"]
        repo.insert_questions(empty, chunk_id, [question(question="Moje vlastní")])

        roundtrip(database, empty, tmp_path)
        assert [r for r in repo.search_questions(empty)
                if r["question"] == "Moje vlastní"]
        assert repo.count_questions(empty) == 4

    def test_it_says_which_documents_still_need_their_pdf(self, database, empty,
                                                          bank, tmp_path):
        # The questions arrive; the pages behind them do not, until the file is
        # found. Saying so is the difference between "broken" and "one step left".
        report = roundtrip(database, empty, tmp_path)
        assert report.needs_pdf == ["book.pdf"]

    def test_a_duplicate_is_judged_on_wording_not_whitespace(
            self, database, empty, bank, tmp_path):
        roundtrip(database, empty, tmp_path)
        existing = repo.search_questions(empty)[0]
        empty.execute("UPDATE questions SET question = ? WHERE id = ?",
                      (f"  {existing['question'].upper()}  ", existing["id"]))
        empty.commit()
        report = roundtrip(database, empty, tmp_path)
        assert report.questions_added == 0


class TestBadFilesAreRefused:
    def test_a_json_file_that_is_not_an_export(self, tmp_path):
        path = tmp_path / "other.json"
        path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
        with pytest.raises(transfer.TransferError, match="not a StudyApp export"):
            transfer.read(path)

    def test_a_file_that_is_not_json_at_all(self, tmp_path):
        path = tmp_path / "book.pdf"
        path.write_bytes(b"%PDF-1.4\nnot json at all")
        with pytest.raises(transfer.TransferError, match="not a JSON file"):
            transfer.read(path)

    def test_an_export_from_a_newer_version(self, tmp_path):
        # Refused rather than guessed at: a format this copy does not know may
        # mean something different by the same field names.
        path = tmp_path / "newer.json"
        path.write_text(json.dumps({transfer.MARKER: transfer.FORMAT + 1,
                                    "documents": []}), encoding="utf-8")
        with pytest.raises(transfer.TransferError, match="Update the app"):
            transfer.read(path)

    def test_an_older_version_is_still_read(self, tmp_path):
        path = tmp_path / "older.json"
        path.write_text(json.dumps({transfer.MARKER: 1, "documents": []}),
                        encoding="utf-8")
        assert transfer.read(path)["documents"] == []

    def test_a_missing_file(self, tmp_path):
        with pytest.raises(transfer.TransferError):
            transfer.read(tmp_path / "nothing-here.json")


class TestADamagedExportIsRefusedNotCrashedOn:
    """The file comes from outside, so every way it can be wrong has to come back
    as a sentence. Under pythonw a KeyError three levels down is a traceback
    nobody sees and a dialog that never appears.
    """

    GOOD_Q = {"type": "open", "question": "Q?", "correct_text": "A",
              "source_quote": "x" * 40, "quote_score": 1.0, "page": 1}
    GOOD_CHUNK = {"ordinal": 0, "page_start": 0, "page_end": 5, "title": "K",
                  "text": "pasáž", "sha256": "c1", "questions": [GOOD_Q]}
    GOOD_DOC = {"filename": "b.pdf", "sha256": "d1", "page_count": 10,
                "language": "cs", "chunks": [GOOD_CHUNK]}

    def write(self, tmp_path, documents):
        path = tmp_path / "probe.json"
        path.write_text(json.dumps(
            {transfer.MARKER: transfer.FORMAT, "documents": documents}),
            encoding="utf-8")
        return path

    def without(self, mapping, key):
        return {k: v for k, v in mapping.items() if k != key}

    @pytest.mark.parametrize("missing", ["sha256"])
    def test_a_document_missing_a_field(self, tmp_path, missing):
        path = self.write(tmp_path, [self.without(self.GOOD_DOC, missing)])
        with pytest.raises(transfer.TransferError, match=missing):
            transfer.read(path)

    @pytest.mark.parametrize("missing",
                             ["sha256", "text", "ordinal", "page_start", "page_end"])
    def test_a_chunk_missing_a_field(self, tmp_path, missing):
        path = self.write(tmp_path, [
            {**self.GOOD_DOC, "chunks": [self.without(self.GOOD_CHUNK, missing)]}])
        with pytest.raises(transfer.TransferError, match=missing):
            transfer.read(path)

    @pytest.mark.parametrize("missing", ["type", "question", "source_quote"])
    def test_a_question_missing_a_field(self, tmp_path, missing):
        path = self.write(tmp_path, [{**self.GOOD_DOC, "chunks": [
            {**self.GOOD_CHUNK,
             "questions": [self.without(self.GOOD_Q, missing)]}]}])
        with pytest.raises(transfer.TransferError, match=missing):
            transfer.read(path)

    def test_the_message_says_where_it_went_wrong(self, tmp_path):
        # A file with 600 questions in it needs more than "invalid".
        path = self.write(tmp_path, [{**self.GOOD_DOC, "chunks": [
            {**self.GOOD_CHUNK, "questions": [
                self.GOOD_Q, self.without(self.GOOD_Q, "question")]}]}])
        with pytest.raises(transfer.TransferError) as caught:
            transfer.read(path)
        assert "b.pdf" in str(caught.value)
        assert "question 2" in str(caught.value)

    @pytest.mark.parametrize("value", ["nope", 5, None, {"a": 1}])
    def test_chunks_that_are_not_a_list(self, tmp_path, value):
        path = self.write(tmp_path, [{**self.GOOD_DOC, "chunks": value}])
        with pytest.raises(transfer.TransferError):
            transfer.read(path)

    def test_a_document_that_is_not_an_object(self, tmp_path):
        for value in ("jsem dokument", None, 42, []):
            path = self.write(tmp_path, [value])
            with pytest.raises(transfer.TransferError, match="expected document"):
                transfer.read(path)

    def test_a_page_number_that_is_not_a_number(self, tmp_path):
        # Accepted once, and written straight into an INTEGER column.
        path = self.write(tmp_path, [{**self.GOOD_DOC, "chunks": [
            {**self.GOOD_CHUNK, "page_start": "první"}]}])
        with pytest.raises(transfer.TransferError, match="page_start"):
            transfer.read(path)

    def test_a_boolean_is_not_a_page_number(self, tmp_path):
        # bool is an int to Python and a page number to nobody.
        path = self.write(tmp_path, [{**self.GOOD_DOC, "chunks": [
            {**self.GOOD_CHUNK, "ordinal": True}]}])
        with pytest.raises(transfer.TransferError, match="ordinal"):
            transfer.read(path)

    def test_an_unknown_question_type(self, tmp_path):
        path = self.write(tmp_path, [{**self.GOOD_DOC, "chunks": [
            {**self.GOOD_CHUNK,
             "questions": [{**self.GOOD_Q, "type": "essay"}]}]}])
        with pytest.raises(transfer.TransferError, match="essay"):
            transfer.read(path)

    def test_options_that_are_not_a_list(self, tmp_path):
        path = self.write(tmp_path, [{**self.GOOD_DOC, "chunks": [
            {**self.GOOD_CHUNK, "questions": [
                {**self.GOOD_Q, "type": "mcq", "options": "ABCD",
                 "correct_index": 1}]}]}])
        with pytest.raises(transfer.TransferError, match="options"):
            transfer.read(path)

    def test_a_real_export_still_passes(self, database, empty, bank, tmp_path):
        # The check must not have grown teeth for the files it exists to accept.
        path = tmp_path / "real.json"
        transfer.write(database, path, None)
        report = transfer.apply(empty, transfer.read(path))
        assert report.questions_added == 3
