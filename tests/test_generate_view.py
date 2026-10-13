"""The Generate tab and the model behind it.

Widget tests earn their place here for one reason: the table is what tells you
whether a run can be resumed, and a column that quietly shows the wrong page
numbers or loses the failure reason would not be noticed until the one moment it
mattered.
"""

from pathlib import Path

import pymupdf
import pytest
from PySide6.QtCore import QMimeData, Qt, QUrl
from PySide6.QtWidgets import QMessageBox

from studyapp.db import connection as db_connection
from studyapp.db import repo
from studyapp.generate.worker import GenerationController
from studyapp.ui.generate_view import GenerateView
from studyapp.ui.models import ChunkTableModel

FIXTURES = Path(__file__).parent / "fixtures"


class FakeChunk:
    def __init__(self, ordinal, page_start, page_end, title=None):
        self.ordinal = ordinal
        self.page_start, self.page_end = page_start, page_end
        self.title = title
        self.text = f"text {ordinal}"
        self.sha256 = f"sha-{ordinal}"


@pytest.fixture
def pdf(tmp_path) -> str:
    path = tmp_path / "book.pdf"
    document = pymupdf.open()
    for _ in range(3):
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(60, 60, 400, 700),
                            "Nějaký souvislý text o databázích. " * 40,
                            fontsize=10, fontname="helv")
    document.save(path)
    document.close()
    return str(path)


@pytest.fixture
def database(tmp_path, pdf):
    path = tmp_path / "view.db"
    db_connection.ensure_schema(path)
    connection = db_connection.connect(path)
    document_id = repo.insert_document(connection, pdf, "book.pdf", "sha-book", 3, "cs")
    repo.insert_chunks(connection, document_id, [
        FakeChunk(0, 0, 4, "1 Úvod"),
        FakeChunk(1, 5, 9, "2 Relační model"),
        FakeChunk(2, 10, 14, None),
    ])
    yield connection
    connection.close()


@pytest.fixture
def rows(database):
    document_id = repo.list_documents(database)[0]["id"]
    return repo.list_chunks(database, document_id)


@pytest.fixture
def chunk_id(database):
    document_id = repo.list_documents(database)[0]["id"]
    return repo.list_chunks(database, document_id)[0]["id"]


@pytest.fixture
def view(qapp, database):
    controller = GenerationController(database)
    widget = GenerateView(database, controller)
    yield widget
    widget.deleteLater()


class TestChunkTableModel:
    def test_shape(self, qapp, rows):
        model = ChunkTableModel()
        model.set_rows(rows)
        assert model.rowCount() == 3
        assert model.columnCount() == 5

    def test_pages_are_shown_one_based(self, qapp, rows):
        # Stored 0-based to match PyMuPDF and QtPdf; a reader counts from 1.
        model = ChunkTableModel()
        model.set_rows(rows)
        assert model.data(model.index(0, ChunkTableModel.PAGES)) == "1–5"

    def test_an_untitled_chunk_shows_a_dash(self, qapp, rows):
        model = ChunkTableModel()
        model.set_rows(rows)
        assert model.data(model.index(2, ChunkTableModel.TITLE)) == "—"

    def test_the_failure_reason_is_the_tooltip(self, qapp, database, rows):
        repo.set_chunk_status(database, rows[0]["id"], "failed", "API 529: overloaded")
        document_id = repo.list_documents(database)[0]["id"]
        model = ChunkTableModel()
        model.set_rows(repo.list_chunks(database, document_id))
        tooltip = model.data(model.index(0, ChunkTableModel.STATUS), Qt.ToolTipRole)
        assert "529" in tooltip

    def test_status_counts(self, qapp, database, rows):
        repo.set_chunk_status(database, rows[0]["id"], "done")
        repo.set_chunk_status(database, rows[1]["id"], "skipped")
        document_id = repo.list_documents(database)[0]["id"]
        model = ChunkTableModel()
        model.set_rows(repo.list_chunks(database, document_id))
        assert model.status_counts() == {"done": 1, "skipped": 1, "pending": 1}

    def test_finding_a_row_by_chunk_id(self, qapp, rows):
        model = ChunkTableModel()
        model.set_rows(rows)
        assert model.row_for_chunk(rows[1]["id"]) == 1
        assert model.row_for_chunk(9999) is None

    def test_empty_model_is_harmless(self, qapp):
        model = ChunkTableModel()
        model.set_rows([])
        assert model.rowCount() == 0
        assert model.question_total() == 0
        assert model.data(model.index(0, 0)) is None


class TestRemovingADocument:
    """Removing a file must never cost you the question bank.

    The rule: a chunk that produced questions is archived, because the questions
    reach their chapter, page, highlight rects and the PDF through it. A chunk
    that produced nothing is deleted outright.
    """

    def test_it_is_offered_only_when_there_is_something_to_remove(self, view):
        assert view.remove_button.isEnabled()

    def test_chunks_with_no_questions_are_deleted_outright(
            self, view, database, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        view.remove_document()

        assert database.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
        assert repo.list_documents(database) == []

    def test_chunks_with_questions_are_archived_and_the_questions_kept(
            self, view, database, monkeypatch, chunk_id):
        repo.insert_questions(database, chunk_id, [_question(), _question()])
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        view.remove_document()

        assert repo.count_questions(database) == 2, "the bank is untouched"
        kept = database.execute(
            "SELECT archived FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        assert kept["archived"] == 1
        # The two empty chunks alongside it had nothing to protect.
        assert database.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1

    def test_the_document_survives_while_any_chunk_does(
            self, view, database, monkeypatch, chunk_id):
        # The questions still need its path to draw the page.
        repo.insert_questions(database, chunk_id, [_question()])
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        view.remove_document()
        assert len(repo.list_documents(database)) == 1

    def test_an_archived_document_leaves_the_generation_list(
            self, view, database, monkeypatch, chunk_id):
        repo.insert_questions(database, chunk_id, [_question()])
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        view.remove_document()

        assert repo.list_documents(database, generatable_only=True) == []
        assert view.documents.count() == 0

    def test_archived_chunks_are_never_generated_again(
            self, view, database, monkeypatch, chunk_id):
        repo.insert_questions(database, chunk_id, [_question()])
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        view.remove_document()

        document_id = repo.list_documents(database)[0]["id"]
        assert repo.get_pending_chunks(database, document_id) == []
        assert repo.list_chunks(database, document_id) == []

    def test_the_kept_questions_still_reach_their_source(
            self, view, database, monkeypatch, chunk_id):
        # The whole reason for archiving rather than deleting: chapter, page and
        # the PDF path all come through the chunk.
        repo.insert_questions(database, chunk_id, [_question()])
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        view.remove_document()

        picked = repo.pick_questions(database, limit=10)
        assert len(picked) == 1
        assert picked[0]["chapter"] == "1 Úvod"
        assert picked[0]["pdf_path"]
        assert picked[0]["page"] == 1

    def test_answers_recorded_against_them_survive_too(
            self, view, database, monkeypatch, chunk_id):
        repo.insert_questions(database, chunk_id, [_question()])
        question_id = database.execute("SELECT id FROM questions").fetchone()["id"]
        repo.record_attempt(database, None, question_id, is_correct=False)

        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        view.remove_document()
        assert database.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
        assert repo.mistakes_count(database) == 1

    def test_the_dialog_says_which_of_the_two_will_happen(
            self, view, database, monkeypatch, chunk_id):
        repo.insert_questions(database, chunk_id, [_question()])
        shown = {}

        def capture(parent, title, text, *args, **kwargs):
            shown["text"] = text
            return QMessageBox.No

        monkeypatch.setattr(QMessageBox, "question", staticmethod(capture))
        view.remove_document()

        assert "2 chunks with no questions will be deleted" in shown["text"]
        assert "1 chunks are kept, archived" in shown["text"]
        assert "No questions are deleted" in shown["text"]

    def test_saying_no_changes_nothing(self, view, database, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.No))
        view.remove_document()
        assert len(repo.list_documents(database)) == 1
        assert database.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 3

    def test_the_blur_cache_goes_only_when_the_document_does(
            self, view, database, monkeypatch, chunk_id):
        from studyapp.ingest import blur

        sha = repo.list_documents(database)[0]["sha256"]
        blur.page_path(sha, 0).write_bytes(b"x")
        repo.insert_questions(database, chunk_id, [_question()])

        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        view.remove_document()
        # A kept question still needs its blurred page to draw the source pane.
        assert blur.cache_size_bytes(sha) > 0
        blur.clear_cache(sha)

    def test_the_blur_cache_is_freed_when_nothing_is_kept(
            self, view, database, monkeypatch):
        from studyapp.ingest import blur

        sha = repo.list_documents(database)[0]["sha256"]
        blur.page_path(sha, 0).write_bytes(b"x")

        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        view.remove_document()
        assert blur.cache_size_bytes(sha) == 0

    def test_the_view_empties_afterwards(self, view, database, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        view.remove_document()

        assert view.documents.count() == 0
        assert view.table.model().rowCount() == 0
        assert not view.remove_button.isEnabled()
        assert view.import_button.isEnabled(), "importing must stay available"

    def test_removing_nothing_selected_is_harmless(self, view):
        view.document_id = None
        view.remove_document()


def _question():
    return {"type": "open", "question": "Otazka?", "correct_text": "a",
            "source_quote": "citace dost dlouha na to aby prosla", "quote_score": 1.0,
            "page": 1}


class TestLocatingAMovedPDF:
    """A database is portable; the path inside it is not.

    `documents.path` is absolute and recorded at import, so it stops being true
    the moment the file is renamed or the database is opened on another machine.
    Everything else survives: pages, quotes and rectangles are offsets into the
    file's contents, not into its location.

    The button that offers to find it lives on the Question bank tab, next to the
    document picker; its tests are in test_stats_and_bank.py.
    """

    @pytest.fixture
    def moved(self, view, database, pdf, tmp_path):
        """The document, with its PDF no longer where it was imported from.

        The hash has to be the file's real one, as a real import would have
        stored -- it is what relocation checks the offered file against.
        """
        from studyapp.ingest.extract import file_sha256

        document_id = repo.list_documents(database)[0]["id"]
        database.execute("UPDATE documents SET path = ?, sha256 = ? WHERE id = ?",
                         (str(tmp_path / "gone" / "book.pdf"), file_sha256(pdf),
                          document_id))
        database.commit()
        view.refresh_chunks()
        return document_id

    def test_the_app_knows_the_file_is_missing(self, moved, database):
        assert not repo.document_is_present(database, moved)

    def test_the_same_file_elsewhere_is_accepted(self, moved, database, pdf, tmp_path):
        import shutil

        copy = tmp_path / "somewhere else.pdf"
        shutil.copy(pdf, copy)
        assert repo.relocate_document(database, moved, copy)
        assert repo.document_is_present(database, moved)

    def test_a_different_pdf_is_refused(self, moved, database, tmp_path):
        # It would keep every stored page number while the text underneath had
        # moved -- the reveal would land confidently on the wrong paragraph,
        # which is worse than the broken path it replaces.
        other = tmp_path / "different.pdf"
        document = pymupdf.open()
        document.new_page()
        document.save(other)
        document.close()

        before = repo.get_document(database, moved)["path"]
        assert not repo.relocate_document(database, moved, other)
        assert repo.get_document(database, moved)["path"] == before

    def test_relocating_an_unknown_document_is_harmless(self, database, pdf):
        assert not repo.relocate_document(database, 9999, pdf)

    def test_the_questions_are_untouched_by_any_of_it(self, moved, database, chunk_id, pdf,
                                                      tmp_path):
        import shutil

        repo.insert_questions(database, chunk_id, [_question()])
        copy = tmp_path / "again.pdf"
        shutil.copy(pdf, copy)
        repo.relocate_document(database, moved, copy)

        picked = repo.pick_questions(database, limit=10)
        assert len(picked) == 1
        assert picked[0]["pdf_path"] == str(copy.resolve())


class TestEmptyDatabase:
    """Before anything has been imported -- the state the app first opens in."""

    @pytest.fixture
    def empty_view(self, qapp, tmp_path):
        path = tmp_path / "empty.db"
        db_connection.ensure_schema(path)
        connection = db_connection.connect(path)
        controller = GenerationController(connection)
        widget = GenerateView(connection, controller)
        yield widget
        widget.deleteLater()
        connection.close()

    def test_import_still_works_with_nothing_imported(self, empty_view):
        # It used to be disabled here: one boolean was carrying both "a run is
        # going" and "there is a document", so an empty database switched off
        # the one button that could fix it.
        assert empty_view.import_button.isEnabled()

    def test_the_other_buttons_are_off(self, empty_view):
        assert not empty_view.start_button.isEnabled()
        assert not empty_view.regenerate_button.isEnabled()
        assert not empty_view.stop_button.isEnabled()

    def test_it_says_so(self, empty_view):
        assert "No document" in empty_view.summary.text()

    def test_dropping_a_pdf_is_still_accepted(self, empty_view, tmp_path):
        # Drag and drop went through the controller directly, so it kept working
        # while the button did not -- which is what made the bug confusing.
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        data = QMimeData()
        data.setUrls([QUrl.fromLocalFile(str(pdf))])
        assert GenerateView._dropped_pdf(_FakeEvent(data)) is not None


class TestGenerateView:
    def test_it_lists_the_imported_document(self, view):
        assert view.documents.count() == 1
        assert "book.pdf" in view.documents.itemText(0)

    def test_the_table_fills_from_the_database(self, view):
        assert view.table.model().rowCount() == 3

    def test_the_summary_counts_what_is_pending(self, view):
        assert "3 pending" in view.summary.text()

    def test_stop_is_disabled_until_a_run_starts(self, view):
        assert view.start_button.isEnabled()
        assert not view.stop_button.isEnabled()

    def test_regenerating_nothing_selected_does_nothing(self, view, database):
        view._on_regenerate()
        document_id = repo.list_documents(database)[0]["id"]
        assert repo.chunk_status_counts(database, document_id) == {"pending": 3}

    def test_regenerating_actually_starts_the_run(self, view, database, monkeypatch):
        # It used to only mark the rows pending and wait for Start, which looks
        # exactly like nothing happening.
        started = []
        monkeypatch.setattr(view.controller, "start",
                            lambda doc, **kw: started.append(kw.get("chunk_ids")))
        document_id = repo.list_documents(database)[0]["id"]
        for row in repo.list_chunks(database, document_id):
            repo.set_chunk_status(database, row["id"], "done")
        view.refresh_chunks()

        view.table.selectRow(1)
        chosen = view.table.model().chunk_id(1)
        view._on_regenerate()

        assert started == [{chosen}], "it should run exactly the selected chunk"

    def test_regenerating_does_nothing_while_a_run_is_going(self, view, monkeypatch):
        monkeypatch.setattr(type(view.controller), "is_running", property(lambda self: True))
        started = []
        monkeypatch.setattr(view.controller, "start", lambda *a, **k: started.append(1))
        view.table.selectRow(0)
        view._on_regenerate()
        assert started == []

    def test_regenerating_a_selection_leaves_the_others_alone(self, view, database):
        # The selected chunk is picked up by the run straight away, so it is no
        # longer `done`; the two beside it must not have moved at all.
        document_id = repo.list_documents(database)[0]["id"]
        for row in repo.list_chunks(database, document_id):
            repo.set_chunk_status(database, row["id"], "done")
        view.refresh_chunks()

        view.table.selectRow(1)
        chosen = view.table.model().chunk_id(1)
        view._on_regenerate()

        statuses = {row["id"]: row["status"]
                    for row in repo.list_chunks(database, document_id)}
        assert statuses.pop(chosen) != "done"
        assert set(statuses.values()) == {"done"}

    def test_a_failure_message_reaches_the_summary(self, view):
        view._on_failed("No way to reach Claude.")
        assert view.summary.text() == "No way to reach Claude."
        assert view.start_button.isEnabled()

    def test_progress_bar_tracks_the_run(self, view):
        view._on_progress(3, 12)
        assert view.progress.value() == 3
        assert view.progress.maximum() == 12

    def test_the_progress_bar_is_hidden_while_idle(self, view):
        # An empty bar at zero is furniture, not information -- and at its
        # default height it reads as a broken widget.
        assert view.progress.isHidden()
        assert view.progress.height() == 2

    def test_it_appears_during_a_run_and_goes_again_after(self, view):
        view._on_progress(1, 4)
        assert not view.progress.isHidden()
        view._on_finished()
        assert view.progress.isHidden()

    def test_import_is_blocked_only_while_a_run_is_going(self, view):
        view._update_buttons(running=True)
        assert not view.import_button.isEnabled()
        assert view.stop_button.isEnabled()
        view._update_buttons(running=False)
        assert view.import_button.isEnabled()


class TestDragAndDrop:
    def test_a_pdf_is_accepted(self, view, pdf):
        data = QMimeData()
        data.setUrls([QUrl.fromLocalFile(pdf)])
        # Qt hands back forward slashes even on Windows; compare as paths.
        assert Path(GenerateView._dropped_pdf(_FakeEvent(data))) == Path(pdf)

    def test_anything_else_is_ignored(self, view, tmp_path):
        other = tmp_path / "notes.txt"
        other.write_text("x", encoding="utf-8")
        data = QMimeData()
        data.setUrls([QUrl.fromLocalFile(str(other))])
        assert GenerateView._dropped_pdf(_FakeEvent(data)) is None

    def test_dropping_plain_text_is_ignored(self, view):
        data = QMimeData()
        data.setText("hello")
        assert GenerateView._dropped_pdf(_FakeEvent(data)) is None


class _FakeEvent:
    """Just enough of a drop event to ask it what was dropped."""

    def __init__(self, mime):
        self._mime = mime

    def mimeData(self):
        return self._mime


class TestTheTabsStayInStepAboutDocuments:
    """A document removed on the Question bank tab used to live on here.

    The two tabs were wired one way only: Generate told the bank about imports,
    the bank told Generate nothing. So removing a document left this picker
    listing it, every action on it failed quietly because the row was gone, and
    switching tabs did not help -- `_on_tab_changed` had no branch for this tab
    at all, which is why it survived until the window was reopened.
    """

    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        from studyapp.ui.main_window import create_window

        path = tmp_path / "window.db"
        monkeypatch.setenv("STUDYAPP_DB", str(path))
        db_connection.ensure_schema(path)
        window = create_window()
        connection = window.connection
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            document_id = repo.insert_document(
                connection, name, name, f"sha-{name}", 10, "cs")
            connection.execute(
                """INSERT INTO chunks (document_id, ordinal, page_start, page_end,
                                       title, text, sha256, status, updated_at)
                   VALUES (?, 0, 0, 5, 'K', 't', ?, 'done', ?)""",
                (document_id, f"chunk-{name}", repo.utcnow()))
            connection.commit()
            chunk_id = connection.execute(
                "SELECT id FROM chunks WHERE sha256 = ?",
                (f"chunk-{name}",)).fetchone()["id"]
            repo.insert_questions(connection, chunk_id, [{
                "type": "open", "question": f"Otázka z {name}",
                "correct_text": "x", "source_quote": "q" * 40,
                "quote_score": 1.0, "page": 1}])
        window.bank_view.reload_documents()
        window.bank_view.refresh()
        window.generate_view.reload_documents()
        qapp.processEvents()
        yield window
        window.connection.close()

    def remove_in_bank(self, window, document_id, monkeypatch, qapp):
        bank = window.bank_view
        bank.documents.setCurrentIndex(bank.documents.findData(document_id))
        monkeypatch.setattr(QMessageBox, "exec", lambda self: None)
        monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: next(
            b for b in self.buttons()
            if self.buttonRole(b) == QMessageBox.DestructiveRole))
        bank._remove_document()
        qapp.processEvents()

    def test_a_removed_document_leaves_the_generate_picker(
            self, qapp, window, monkeypatch):
        generate = window.generate_view
        victim = repo.list_documents(window.connection)[0]["id"]
        assert generate.documents.findData(victim) >= 0

        self.remove_in_bank(window, victim, monkeypatch, qapp)

        assert generate.documents.findData(victim) < 0, "the picker still lists it"
        assert generate.document_id != victim, "and is still pointed at it"

    def test_the_document_it_is_pointed_at_really_exists(
            self, qapp, window, monkeypatch):
        generate = window.generate_view
        victim = repo.list_documents(window.connection)[0]["id"]
        generate.documents.setCurrentIndex(generate.documents.findData(victim))
        qapp.processEvents()

        self.remove_in_bank(window, victim, monkeypatch, qapp)

        assert repo.get_document(window.connection, generate.document_id) is not None

    def test_removing_the_last_one_empties_the_tab_cleanly(
            self, qapp, window, monkeypatch):
        for row in list(repo.list_documents(window.connection)):
            self.remove_in_bank(window, row["id"], monkeypatch, qapp)

        generate = window.generate_view
        assert generate.documents.count() == 0
        assert generate.document_id is None
        assert generate.table.model().rowCount() == 0
        generate.refresh_chunks()           # must not raise

    def test_an_unrelated_bank_change_does_not_move_this_tab(self, qapp, window):
        # Starring a question must not send the reader to a different book.
        generate = window.generate_view
        chosen = repo.list_documents(window.connection)[1]["id"]
        generate.documents.setCurrentIndex(generate.documents.findData(chosen))
        qapp.processEvents()

        first = repo.search_questions(window.connection)[0]
        repo.set_starred(window.connection, first["id"], True)
        window.bank_view.changed.emit()
        qapp.processEvents()

        assert generate.document_id == chosen

    def test_switching_to_the_tab_refreshes_it(self, qapp, window):
        generate = window.generate_view
        victim = repo.list_documents(window.connection)[0]["id"]
        # Deleted behind the tab's back, as another window or a CLI would.
        repo.delete_document(window.connection, victim)

        window.tabs.setCurrentIndex(window.tabs.indexOf(generate))
        qapp.processEvents()

        assert generate.documents.findData(victim) < 0
