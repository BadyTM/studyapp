"""Statistics and question-bank management.

The statistics half is mostly about one claim: every figure is derived from
`attempts` on the way in, so there is no stored summary that can disagree with
the answers it summarises.

The bank half is about the three things that must not happen -- editing a quote,
editing options as raw JSON, and deleting a row that has history behind it.
"""

import json

import pymupdf
import pytest
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QMessageBox

from studyapp.db import connection as db_connection
from studyapp.db import repo
from studyapp.ui.bank_view import BankView, EditDialog, QuestionTableModel
from studyapp.ui.stats_view import StatsView, local_date, percent


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "bank.db"
    db_connection.ensure_schema(path)
    connection = db_connection.connect(path)
    document_id = repo.insert_document(connection, "b.pdf", "b.pdf", "sha", 200, "cs")
    for ordinal, title, page in [(0, "1 Úvod", 0), (1, "6 Transakce", 40)]:
        connection.execute(
            """INSERT INTO chunks (document_id, ordinal, page_start, page_end, title,
                                   text, sha256, status, updated_at)
               VALUES (?, ?, ?, ?, ?, 't', ?, 'done', ?)""",
            (document_id, ordinal, page, page + 5, title, f"sha{ordinal}",
             repo.utcnow()))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def chunks(database):
    return [row["id"] for row in database.execute("SELECT id FROM chunks ORDER BY id")]


def mcq(**overrides):
    base = {
        "type": "mcq",
        "question": "Která vlastnost zaručuje trvalost?",
        "options": ["Atomicita", "Konzistence", "Izolace", "Trvalost"],
        "correct_index": 3,
        "explanation": "Trvalost.",
        "source_quote": "Trvalost znamená, že změny zůstávají zachovány.",
        "quote_score": 1.0,
        "page": 43,
        "rects": [[72.0, 100.0, 400.0, 114.0]],
    }
    base.update(overrides)
    return base


# --- statistics -------------------------------------------------------------


class TestPercent:
    def test_ordinary(self):
        assert percent(3, 4) == "75%"

    def test_nothing_answered_is_a_dash_not_a_zero(self):
        # 0% reads as "you got them all wrong"; nothing was answered at all.
        assert percent(0, 0) == "—"


class TestLocalDate:
    def test_a_stored_utc_stamp_becomes_a_readable_local_time(self):
        assert local_date("2026-03-01T12:00:00+00:00") != "—"

    def test_missing_and_broken_stamps_do_not_crash(self):
        assert local_date(None) == "—"
        assert local_date("not a date") == "not a date"


class TestChapterAccuracy:
    def test_attempts_are_grouped_by_chapter(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        repo.insert_questions(database, chunks[1], [mcq(), mcq()])
        first = database.execute("SELECT id FROM questions ORDER BY id").fetchall()

        repo.record_attempt(database, None, first[0]["id"], is_correct=True)
        repo.record_attempt(database, None, first[1]["id"], is_correct=False)
        repo.record_attempt(database, None, first[2]["id"], is_correct=False)

        by_chapter = {row["chapter"]: row for row in repo.accuracy_by_chapter(database)}
        assert by_chapter["1 Úvod"]["correct"] == 1
        assert by_chapter["6 Transakce"]["attempts"] == 2
        assert by_chapter["6 Transakce"]["correct"] == 0

    def test_every_attempt_counts_not_just_the_latest(self, database, chunks):
        # A chapter answered wrong three times and right once is a weak chapter,
        # even though its mistakes queue is empty.
        repo.insert_questions(database, chunks[0], [mcq()])
        question_id = database.execute("SELECT id FROM questions").fetchone()["id"]
        for correct in (False, False, False, True):
            repo.record_attempt(database, None, question_id, is_correct=correct)

        row = repo.accuracy_by_chapter(database)[0]
        assert row["attempts"] == 4 and row["correct"] == 1
        assert repo.mistakes_count(database) == 0

    def test_a_chapter_with_no_attempts_still_appears(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        rows = repo.accuracy_by_chapter(database)
        assert rows[0]["attempts"] == 0
        assert rows[0]["questions"] == 1

    def test_retired_questions_are_left_out(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        database.execute("UPDATE questions SET is_active = 0 WHERE id = 1")
        database.commit()
        assert repo.accuracy_by_chapter(database)[0]["questions"] == 1


class TestSessionHistory:
    def test_a_session_with_answers_is_listed(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        question_id = database.execute("SELECT id FROM questions").fetchone()["id"]
        session_id = repo.start_session(database, ("mcq",), "all")
        repo.record_attempt(database, session_id, question_id, is_correct=True)

        history = repo.session_history(database)
        assert len(history) == 1
        assert history[0]["answered"] == 1 and history[0]["correct"] == 1

    def test_an_abandoned_empty_session_is_not_listed(self, database):
        # Opened and closed without answering anything is noise, not history.
        repo.start_session(database, ("mcq",), "all")
        assert repo.session_history(database) == []


class TestStatsView:
    def test_it_reads_the_bank_on_refresh(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        question_id = database.execute("SELECT MIN(id) AS id FROM questions").fetchone()["id"]
        repo.record_attempt(database, None, question_id, is_correct=True)

        view = StatsView(database)
        view.refresh()
        assert view.counters["bank"].value.text() == "2"
        assert view.counters["accuracy"].value.text() == "100%"
        assert view.counters["practised"].value.text() == "1"

    def test_an_empty_database_shows_dashes_rather_than_crashing(self, qapp, database):
        view = StatsView(database)
        view.refresh()
        assert view.counters["accuracy"].value.text() == "—"
        assert view.chapters.rowCount() == 0

    def test_the_weakest_chapter_ignores_a_tiny_sample(self, qapp, database, chunks):
        # Two wrong out of two is not a weak chapter, it is a small sample.
        repo.insert_questions(database, chunks[0], [mcq()])
        question_id = database.execute("SELECT id FROM questions").fetchone()["id"]
        repo.record_attempt(database, None, question_id, is_correct=False)
        repo.record_attempt(database, None, question_id, is_correct=False)

        view = StatsView(database)
        view.refresh()
        assert view.weakest.text() == ""

    def test_the_weakest_chapter_is_named_once_there_is_enough(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[1], [mcq()])
        question_id = database.execute("SELECT id FROM questions").fetchone()["id"]
        for _ in range(6):
            repo.record_attempt(database, None, question_id, is_correct=False)

        view = StatsView(database)
        view.refresh()
        assert "6 Transakce" in view.weakest.text()


class TestResettingProgress:
    """Deleting the history without touching what it was history of."""

    @pytest.fixture
    def practised(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        first, second = [row["id"] for row in
                         database.execute("SELECT id FROM questions ORDER BY id")]
        session_id = repo.start_session(database, ["mcq"], "all")
        repo.record_attempt(database, session_id, first, is_correct=True)
        repo.record_attempt(database, session_id, second, is_correct=False)
        repo.set_starred(database, second, True)
        return first, second

    def test_it_reports_what_it_would_erase_before_erasing_it(self, database, practised):
        assert repo.progress_totals(database) == {
            "attempts": 2, "sessions": 1, "questions": 2}

    def test_the_questions_survive(self, database, practised):
        repo.reset_progress(database)
        assert repo.count_questions(database) == 2

    def test_the_history_is_gone(self, database, practised):
        erased = repo.reset_progress(database)
        assert erased == {"attempts": 2, "sessions": 1, "questions": 2}
        assert repo.progress_totals(database) == {
            "attempts": 0, "sessions": 0, "questions": 0}

    def test_the_mistakes_queue_empties_with_it(self, database, practised):
        assert repo.mistakes_count(database) == 1
        repo.reset_progress(database)
        assert repo.mistakes_count(database) == 0

    def test_stars_are_not_history_and_stay(self, database, practised):
        _, second = practised
        repo.reset_progress(database)
        row = database.execute("SELECT starred FROM questions WHERE id = ?",
                               (second,)).fetchone()
        assert row["starred"] == 1

    def test_the_counters_go_back_to_empty(self, qapp, database, practised):
        view = StatsView(database)
        view.refresh()
        assert view.counters["attempts"].value.text() == "2"

        repo.reset_progress(database)
        view.refresh()
        assert view.counters["attempts"].value.text() == "0"
        assert view.counters["practised"].value.text() == "0"
        assert view.counters["accuracy"].value.text() == "—"
        assert view.counters["bank"].value.text() == "2"      # the bank is intact
        assert view.sessions.rowCount() == 0
        # The chapter is still there -- it has questions -- but no answers.
        assert [row["attempts"] for row in repo.accuracy_by_chapter(database)] == [0]

    def test_declining_the_confirmation_changes_nothing(self, qapp, database, practised,
                                                        monkeypatch):
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.No))
        view = StatsView(database)
        view.reset_progress()
        assert repo.progress_totals(database)["attempts"] == 2

    def test_accepting_it_clears_and_announces(self, qapp, database, practised,
                                               monkeypatch):
        seen = []
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        monkeypatch.setattr(QMessageBox, "information",
                            staticmethod(lambda parent, title, text, *a: seen.append(text)))
        view = StatsView(database)
        heard = []
        view.changed.connect(lambda: heard.append(True))

        view.reset_progress()

        assert repo.progress_totals(database)["attempts"] == 0
        assert heard == [True]
        assert "2" in seen[0]

    def test_it_says_so_rather_than_asking_when_there_is_nothing_to_clear(
            self, qapp, database, chunks, monkeypatch):
        repo.insert_questions(database, chunks[0], [mcq()])
        asked = []
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: asked.append(True)))
        monkeypatch.setattr(QMessageBox, "information",
                            staticmethod(lambda *a, **k: None))
        view = StatsView(database)
        view.reset_progress()
        assert asked == []


# --- the question bank ------------------------------------------------------


class TestSearching:
    def test_free_text_matches_the_question(self, database, chunks):
        repo.insert_questions(database, chunks[0],
                              [mcq(), mcq(question="Co je index?")])
        assert len(repo.search_questions(database, text="index")) == 1

    def test_free_text_matches_inside_the_options(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        assert len(repo.search_questions(database, text="Konzistence")) == 1

    def test_needs_review_finds_an_unplaced_quote(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq(), mcq(page=None)])
        found = repo.search_questions(database, state="review")
        assert len(found) == 1 and found[0]["page"] is None

    def test_needs_review_finds_a_weak_quote_score(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq(), mcq(quote_score=0.91)])
        assert len(repo.search_questions(database, state="review")) == 1

    def test_retired_questions_are_hidden_by_default(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        repo.set_active(database, [1], False)
        assert len(repo.search_questions(database, state="active")) == 1
        assert len(repo.search_questions(database, state="inactive")) == 1
        assert len(repo.search_questions(database, state="all")) == 2

    def test_filtering_by_chapter(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        repo.insert_questions(database, chunks[1], [mcq(), mcq()])
        assert len(repo.search_questions(database, chapter="6 Transakce")) == 2


class TestEditing:
    def test_an_edit_is_saved_and_stamped(self, database, chunks):
        # `edited_at` is what stops a regeneration of this chunk discarding it.
        repo.insert_questions(database, chunks[0], [mcq()])
        question_id = database.execute("SELECT id FROM questions").fetchone()["id"]

        repo.update_question(database, question_id, question="Opravená otázka?")
        row = database.execute("SELECT * FROM questions WHERE id = ?",
                               (question_id,)).fetchone()
        assert row["question"] == "Opravená otázka?"
        assert row["edited_at"] is not None

    def test_an_edited_question_survives_regeneration(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        question_id = database.execute("SELECT MIN(id) AS id FROM questions").fetchone()["id"]
        repo.update_question(database, question_id, question="Ručně opravená?")

        repo.deactivate_unedited_questions(database, chunks[0])
        row = database.execute("SELECT * FROM questions WHERE id = ?",
                               (question_id,)).fetchone()
        assert row["is_active"] == 1

    def test_the_quote_cannot_be_edited(self, database, chunks):
        # Not in EDITABLE_FIELDS: a hand-edited quote no longer matches the text
        # it was located in, which breaks the highlight without saying so.
        repo.insert_questions(database, chunks[0], [mcq()])
        question_id = database.execute("SELECT id FROM questions").fetchone()["id"]
        before = database.execute("SELECT source_quote FROM questions").fetchone()[0]

        repo.update_question(database, question_id, source_quote="něco jiného")
        after = database.execute("SELECT source_quote FROM questions").fetchone()[0]
        assert after == before

    def test_the_page_and_rects_cannot_be_edited(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        question_id = database.execute("SELECT id FROM questions").fetchone()["id"]
        repo.update_question(database, question_id, page=1, rects_json="[]")
        row = database.execute("SELECT page, rects_json FROM questions").fetchone()
        assert row["page"] == 43 and row["rects_json"] is not None

    def test_an_update_of_nothing_editable_changes_nothing(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        question_id = database.execute("SELECT id FROM questions").fetchone()["id"]
        assert repo.update_question(database, question_id, page=7) is False
        assert database.execute("SELECT edited_at FROM questions").fetchone()[0] is None

    def test_retiring_is_not_treated_as_an_edit(self, database, chunks):
        # Otherwise retiring a question would also protect it from ever being
        # regenerated, which is not what the reader asked for.
        repo.insert_questions(database, chunks[0], [mcq()])
        question_id = database.execute("SELECT id FROM questions").fetchone()["id"]
        repo.set_active(database, [question_id], False)
        assert database.execute("SELECT edited_at FROM questions").fetchone()[0] is None


class TestEditDialog:
    @pytest.fixture
    def row(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        return repo.search_questions(database)[0]

    def test_options_and_the_correct_index_are_saved_together(self, qapp, row):
        # The reason options are not edited as raw JSON in a grid cell: these two
        # have to agree, and here they cannot get out of step.
        dialog = EditDialog(row)
        dialog.options[0].setText("Přejmenovaná varianta")
        dialog.correct_group.button(0).setChecked(True)

        changes = dialog.changes()
        assert json.loads(changes["options_json"])[0] == "Přejmenovaná varianta"
        assert changes["correct_index"] == 0

    def test_an_empty_question_is_refused(self, qapp, row):
        dialog = EditDialog(row)
        dialog.question.setPlainText("   ")
        assert "cannot be empty" in dialog.problem()

    def test_duplicate_options_are_refused(self, qapp, row):
        dialog = EditDialog(row)
        dialog.options[0].setText("Stejné")
        dialog.options[1].setText("Stejné")
        assert "identical" in dialog.problem()

    def test_too_few_options_are_refused(self, qapp, row):
        dialog = EditDialog(row)
        for field in dialog.options[1:]:
            field.setText("")
        assert "at least two" in dialog.problem()

    def test_marking_an_empty_option_as_correct_is_refused(self, qapp, row):
        dialog = EditDialog(row)
        dialog.options[3].setText("")
        dialog.correct_group.button(3).setChecked(True)
        assert dialog.problem() is not None

    def test_a_valid_edit_has_no_problem(self, qapp, row):
        assert EditDialog(row).problem() is None

    def test_the_quote_field_is_read_only(self, qapp, row):
        dialog = EditDialog(row)
        quotes = [child for child in dialog.findChildren(type(dialog.question))
                  if child.isReadOnly()]
        assert quotes, "the source quote must be shown read-only"
        assert row["source_quote"] in quotes[0].toPlainText()


class TestPurging:
    def test_a_retired_question_with_history_is_never_deleted(self, database, chunks):
        # Attempts hang off it; deleting would take part of the record with it.
        repo.insert_questions(database, chunks[0], [mcq()])
        question_id = database.execute("SELECT id FROM questions").fetchone()["id"]
        repo.record_attempt(database, None, question_id, is_correct=False)
        repo.set_active(database, [question_id], False)

        assert repo.purgeable_count(database) == 0
        assert repo.purge_inactive(database) == 0
        assert repo.count_questions(database, active_only=False) == 1

    def test_a_retired_question_with_no_history_can_go(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        repo.set_active(database, [1], False)
        assert repo.purgeable_count(database) == 1
        assert repo.purge_inactive(database) == 1
        assert repo.count_questions(database, active_only=False) == 1

    def test_active_questions_are_never_purged(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        assert repo.purge_inactive(database) == 0


class TestBankView:
    def test_it_opens_on_the_whole_active_bank(self, qapp, database, chunks):
        # Not on "needs review": when generation goes well nothing needs
        # reviewing, and an empty table beside a full bank reads as a fault.
        repo.insert_questions(database, chunks[0], [mcq(), mcq(page=None)])
        view = BankView(database)
        assert view.state.currentData() == "active"
        assert view.model.rowCount() == 2

    def test_the_review_filter_carries_its_count(self, qapp, database, chunks):
        # What the default was really for: telling you whether to bother.
        repo.insert_questions(database, chunks[0], [mcq(), mcq(page=None)])
        view = BankView(database)
        index = view.state.findData("review")
        assert view.state.itemText(index) == "Needs review (1)"

    def test_a_clean_bank_leaves_the_filter_unadorned(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        view = BankView(database)
        assert view.state.itemText(view.state.findData("review")) == "Needs review"
        assert "worth reviewing" not in view.count.text()

    def test_a_filter_matching_nothing_says_so(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        view = BankView(database)
        view.state.setCurrentIndex(view.state.findData("review"))
        assert view.model.rowCount() == 0
        assert "nothing matches this filter" in view.count.text()

    def test_an_empty_bank_does_not_claim_a_filter_problem(self, qapp, database):
        view = BankView(database)
        assert view.model.rowCount() == 0
        assert "nothing matches" not in view.count.text()

    def test_searching_narrows_the_table(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0],
                              [mcq(), mcq(question="Co je index?")])
        view = BankView(database)
        assert view.model.rowCount() == 2
        view.search.setText("index")
        assert view.model.rowCount() == 1

    def test_pages_are_shown_one_based(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        view = BankView(database)
        index = view.model.index(0, QuestionTableModel.PAGE)
        assert view.model.data(index) == 44

    def test_an_unplaced_page_shows_a_dash(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq(page=None)])
        view = BankView(database)
        index = view.model.index(0, QuestionTableModel.PAGE)
        assert view.model.data(index) == "—"

    def test_the_tooltip_carries_the_quote_and_the_options(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        view = BankView(database)
        tooltip = view.model.data(view.model.index(0, 0), Qt.ToolTipRole)
        assert "Trvalost znamená" in tooltip
        assert "←" in tooltip, "the correct option should be marked"

    def test_retiring_from_the_view(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        view = BankView(database)
        view.table.selectRow(0)
        view._set_active(False)
        assert repo.count_questions(database, active_only=True) == 1

    def test_retiring_nothing_selected_does_nothing(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        view = BankView(database)
        view._set_active(False)
        assert repo.count_questions(database, active_only=True) == 1

    def test_a_mixed_selection_stars_all_the_same_way(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        rows = database.execute("SELECT id FROM questions ORDER BY id").fetchall()
        repo.set_starred(database, rows[1]["id"], True)

        view = BankView(database)
        view.table.selectAll()
        view._toggle_star()
        starred = [row["starred"] for row in
                   database.execute("SELECT starred FROM questions ORDER BY id")]
        assert len(set(starred)) == 1, "a mixed selection must not flip each way"


def second_document(connection, path="c.pdf", title="9 Pamět"):
    """A second book in the same bank, with one chunk and its own chapter."""
    document_id = repo.insert_document(connection, path, path, f"sha-{path}", 80, "cs")
    connection.execute(
        """INSERT INTO chunks (document_id, ordinal, page_start, page_end, title,
                               text, sha256, status, updated_at)
           VALUES (?, 0, 0, 5, ?, 't', ?, 'done', ?)""",
        (document_id, title, f"sha-chunk-{path}", repo.utcnow()))
    connection.commit()
    chunk_id = connection.execute(
        "SELECT id FROM chunks WHERE document_id = ?", (document_id,)).fetchone()["id"]
    return document_id, chunk_id


class TestChoosingTheDocument:
    """The bank can hold several books, and almost nothing about one applies to
    another: the chapters differ, and a page number only means anything against
    the file it was found in. So the document is chosen first.
    """

    def test_a_single_document_bank_offers_no_choice(self, qapp, database, chunks):
        # A choice between one book and that same book is noise.
        repo.insert_questions(database, chunks[0], [mcq()])
        view = BankView(database)
        assert view.documents.count() == 1
        assert view.document_id == repo.list_documents(database)[0]["id"]

    def test_two_documents_offer_all_plus_each(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        _, other_chunk = second_document(database)
        repo.insert_questions(database, other_chunk, [mcq(question="Co je pamět?")])
        view = BankView(database)
        assert view.documents.itemData(0) is None, "All documents comes first"
        assert view.documents.count() == 3
        assert view.document_id is None, "and is the default"

    def test_picking_one_document_hides_the_other_questions(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        other_id, other_chunk = second_document(database)
        repo.insert_questions(database, other_chunk, [mcq(question="Co je pamět?")])

        view = BankView(database)
        assert view.model.rowCount() == 3, "all documents to start with"

        view.documents.setCurrentIndex(view.documents.findData(other_id))
        assert view.model.rowCount() == 1
        assert view.model.row_at(0)["question"] == "Co je pamět?"

    def test_the_counts_follow_the_chosen_document(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        other_id, other_chunk = second_document(database)
        repo.insert_questions(database, other_chunk, [mcq(question="Co je pamět?")])

        view = BankView(database)
        view.documents.setCurrentIndex(view.documents.findData(other_id))
        # "1 shown of 3 active" would be a lie about which bank is on screen.
        assert "1 shown · 1 active" in view.count.text()

    def test_the_chapter_filter_follows_the_document(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        other_id, other_chunk = second_document(database)
        repo.insert_questions(database, other_chunk, [mcq(question="Co je pamět?")])

        view = BankView(database)
        view.documents.setCurrentIndex(view.documents.findData(other_id))
        titles = [view.chapter_filter.itemData(i)
                  for i in range(view.chapter_filter.count())]
        assert titles == [None, "9 Pamět"], "the other book's chapters do not apply"

    def test_a_chapter_the_new_document_lacks_falls_back_to_any(
            self, qapp, database, chunks):
        # Otherwise the table would silently show nothing, which reads as a fault.
        repo.insert_questions(database, chunks[1], [mcq()])
        other_id, other_chunk = second_document(database)
        repo.insert_questions(database, other_chunk, [mcq(question="Co je pamět?")])

        view = BankView(database)
        view.chapter_filter.setCurrentIndex(view.chapter_filter.findData("6 Transakce"))
        view.documents.setCurrentIndex(view.documents.findData(other_id))

        assert view.chapter_filter.currentData() is None
        assert view.model.rowCount() == 1


class TestLocatingFromTheBank:
    """Finding a moved PDF moved here from the Generate tab, because this is
    where a document is chosen.
    """

    @pytest.fixture
    def moved(self, database, chunks, tmp_path):
        repo.insert_questions(database, chunks[0], [mcq()])
        document_id = repo.list_documents(database)[0]["id"]
        database.execute("UPDATE documents SET path = ? WHERE id = ?",
                         (str(tmp_path / "gone" / "b.pdf"), document_id))
        database.commit()
        return document_id

    def test_it_offers_to_find_a_missing_file(self, qapp, database, moved):
        view = BankView(database)
        assert not view.locate_button.isHidden()

    def test_the_offer_is_absent_while_the_file_is_there(
            self, qapp, database, chunks, tmp_path):
        present = tmp_path / "b.pdf"
        present.write_bytes(b"%PDF-1.4\n")
        database.execute("UPDATE documents SET path = ?", (str(present),))
        database.commit()
        repo.insert_questions(database, chunks[0], [mcq()])
        view = BankView(database)
        assert view.locate_button.isHidden()

    def test_the_missing_file_is_named_as_such_in_the_picker(
            self, qapp, database, moved):
        second_document(database)       # so the picker is shown at all
        view = BankView(database)
        index = view.documents.findData(moved)
        assert "file missing" in view.documents.itemText(index)

    def test_no_offer_while_all_documents_is_chosen(self, qapp, database, moved):
        # There is no single file to go and find.
        second_document(database)
        view = BankView(database)
        assert view.document_id is None
        assert view.locate_button.isHidden()

    def test_relocating_announces_it_so_the_previews_can_be_rebuilt(
            self, qapp, database, moved, tmp_path, monkeypatch):
        import shutil

        from studyapp.ingest.extract import file_sha256

        real = tmp_path / "real.pdf"
        document = pymupdf.open()
        document.new_page()
        document.save(real)
        document.close()
        database.execute("UPDATE documents SET sha256 = ? WHERE id = ?",
                         (file_sha256(real), moved))
        database.commit()

        elsewhere = tmp_path / "moved away.pdf"
        shutil.copy(real, elsewhere)

        view = BankView(database)
        heard = []
        view.document_relocated.connect(heard.append)
        monkeypatch.setattr("studyapp.ui.bank_view.QFileDialog.getOpenFileName",
                            lambda *a, **kw: (str(elsewhere), ""))
        view.locate_document()

        assert heard == [moved], "the Generate tab needs this to re-render the blur"
        assert repo.document_is_present(database, moved)
        assert view.locate_button.isHidden()


class TestFiltersLiveInTheHeader:
    """The filters sit under the column they act on.

    That is the whole point of putting them there: the heading says what the
    control does, so none of them needs a label of its own.
    """

    def view(self, database, chunks):
        repo.insert_questions(database, chunks[0], [
            mcq(),
            # A word that appears nowhere in the other question, including
            # its explanation and its options -- the search covers all four.
            mcq(type="open", question="Vysvetli serializovatelnost",
                options=None, correct_index=None, explanation=None,
                correct_text="Poradi operaci odpovida seriovemu."),
        ])
        view = BankView(database)
        view.resize(1100, 600)
        view.show()
        return view

    def test_each_filter_is_under_its_own_column(self, qapp, database, chunks):
        view = self.view(database, chunks)
        qapp.processEvents()
        filters = view.table.filters
        header = view.table.horizontalHeader()

        for column, widget in (
            (QuestionTableModel.STAR, view.starred_only),
            (QuestionTableModel.TYPE, view.type_filter),
            (QuestionTableModel.CHAPTER, view.chapter_filter),
            (QuestionTableModel.QUESTION, view.search),
        ):
            assert filters.widget(column) is widget
            left = header.sectionViewportPosition(column)
            width = header.sectionSize(column)
            assert left <= widget.x(), f"column {column} starts after its filter"
            assert widget.x() + widget.width() <= left + width, \
                f"the filter for column {column} spills into the next one"

    def test_page_and_score_have_no_filter(self, qapp, database, chunks):
        # Empty space under a heading says "nothing to filter here" better than
        # a disabled control does.
        view = self.view(database, chunks)
        assert view.table.filters.widget(QuestionTableModel.PAGE) is None
        assert view.table.filters.widget(QuestionTableModel.SCORE) is None

    def test_the_strip_does_not_cover_the_first_row(self, qapp, database, chunks):
        view = self.view(database, chunks)
        qapp.processEvents()
        table = view.table
        assert table.filters.geometry().bottom() <= table.viewportMargins().top()
        assert table.viewportMargins().top() == (
            table.horizontalHeader().height() + table.filters.height())

    def test_filtering_from_the_header_works(self, qapp, database, chunks):
        view = self.view(database, chunks)
        assert view.model.rowCount() == 2
        view.type_filter.setCurrentIndex(view.type_filter.findData("open"))
        assert view.model.rowCount() == 1
        assert view.model.row_at(0)["type"] == "open"

    def test_searching_from_the_header_works(self, qapp, database, chunks):
        view = self.view(database, chunks)
        view.search.setText("serializovatelnost")
        assert view.model.rowCount() == 1

    def test_the_star_filter_sits_under_the_star_column(self, qapp, database, chunks):
        view = self.view(database, chunks)
        ids = [row["id"] for row in repo.search_questions(database)]
        repo.set_starred(database, ids[0], True)
        view.refresh()
        assert view.model.rowCount() == 2
        view.starred_only.setChecked(True)
        assert view.model.rowCount() == 1

    def test_the_filters_follow_a_resized_column(self, qapp, database, chunks):
        view = self.view(database, chunks)
        qapp.processEvents()
        header = view.table.horizontalHeader()
        header.resizeSection(QuestionTableModel.CHAPTER, 320)
        qapp.processEvents()
        left = header.sectionViewportPosition(QuestionTableModel.CHAPTER)
        assert left <= view.chapter_filter.x()
        assert view.chapter_filter.x() + view.chapter_filter.width() <= left + 320

    def test_a_table_that_asks_for_no_filters_is_unchanged(self, qapp, database):
        # The Generate and Progress tables share HoverTableView and must not
        # grow a strip they never asked for.
        from studyapp.ui.tables import HoverTableView

        plain = HoverTableView()
        plain.setModel(QuestionTableModel(database))
        plain.resize(600, 400)
        plain.show()
        qapp.processEvents()
        assert plain._filters is None
        assert plain.viewportMargins().top() == plain.horizontalHeader().height()


class TestTheFilterStripLooksRight:
    """The first attempt at the strip was laid out correctly and looked wrong,
    which for a header is the same as being wrong.
    """

    def view(self, database, chunks, qapp, width=1400):
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        view = BankView(database)
        view.resize(width, 600)
        view.show()
        qapp.processEvents()
        return view

    def test_no_filter_is_clipped_by_its_column(self, qapp, database, chunks):
        # "Type" is sized to the word `mcq`, which left its combo box showing an
        # arrow and half a letter. The column is sized for the control now.
        view = self.view(database, chunks, qapp)
        for widget in (view.type_filter, view.chapter_filter, view.search):
            assert widget.width() >= widget.sizeHint().width(), \
                f"{widget.objectName() or type(widget).__name__} is clipped"

    def test_a_combo_does_not_demand_the_width_of_its_longest_entry(
            self, qapp, database, chunks):
        # The chapter combo asked for 878 pixels -- the width of a chapter title.
        view = self.view(database, chunks, qapp)
        assert view.chapter_filter.sizeHint().width() < 200

    def test_the_table_does_not_scroll_sideways_at_a_normal_width(
            self, qapp, database, chunks):
        # Chapter sized to its contents pushed Question off the edge.
        view = self.view(database, chunks, qapp)
        assert view.table.horizontalScrollBar().maximum() == 0

    def test_the_headings_line_up_with_their_columns(self, qapp, database, chunks):
        # Qt centres headings; the cells and the filters are left-aligned, so
        # every heading floated out of line with both.
        model = QuestionTableModel(database)
        left = int(Qt.AlignLeft | Qt.AlignVCenter)
        right = int(Qt.AlignRight | Qt.AlignVCenter)
        for column in (QuestionTableModel.TYPE, QuestionTableModel.CHAPTER,
                       QuestionTableModel.QUESTION):
            assert model.headerData(column, Qt.Horizontal,
                                    Qt.TextAlignmentRole) == left
        for column in (QuestionTableModel.PAGE, QuestionTableModel.SCORE):
            assert model.headerData(column, Qt.Horizontal,
                                    Qt.TextAlignmentRole) == right

    def test_the_heading_matches_the_cells_below_it(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        model = QuestionTableModel(database)
        model.reload()
        for column in range(len(QuestionTableModel.COLUMNS)):
            cell = model.data(model.index(0, column), Qt.TextAlignmentRole)
            heading = model.headerData(column, Qt.Horizontal, Qt.TextAlignmentRole)
            if cell is not None:
                assert cell == heading, f"column {column} heading fights its cells"

    def test_the_star_box_is_centred_under_its_column(self, qapp, database, chunks):
        # Stretched to fill the column it would only sit against the left edge.
        view = self.view(database, chunks, qapp)
        header = view.table.horizontalHeader()
        left = header.sectionViewportPosition(QuestionTableModel.STAR)
        width = header.sectionSize(QuestionTableModel.STAR)
        box = view.starred_only
        slack_left = box.x() - left
        slack_right = (left + width) - (box.x() + box.width())
        assert abs(slack_left - slack_right) <= 1, "the box is off centre"

    def test_the_strip_is_not_taller_than_two_rows(self, qapp, database, chunks):
        # At the padding the rest of the app uses it stood taller than three
        # rows of the table it is a heading for.
        view = self.view(database, chunks, qapp)
        assert view.table.filters.height() <= 2 * view.table.rowHeight(0)


class TestTheStripIsVisiblyASeparateBand:
    """It was laid out right and could not be seen.

    The first version painted the strip in the card's own paper and closed it
    with a one-pixel #e2dfd8 line against #fbfaf7 -- measurably there, and
    invisible on screen, so the filters read as the first row of questions.
    """

    def strip_image(self, database, chunks, qapp):
        from studyapp.ui import style

        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        view = BankView(database)
        view.setStyleSheet(style.stylesheet())
        view.resize(1200, 500)
        view.show()
        qapp.processEvents()

        strip = view.table.filters
        pixmap = strip.grab()
        ratio = pixmap.devicePixelRatio() or 1.0
        header = view.table.horizontalHeader()
        # The middle of a column with no filter in it, so nothing is in the way.
        x = (header.sectionViewportPosition(QuestionTableModel.PAGE)
             + header.sectionSize(QuestionTableModel.PAGE) // 2)
        return pixmap.toImage(), strip, int(x * ratio), ratio

    def test_a_visible_line_closes_the_strip(self, qapp, database, chunks):
        from studyapp.ui import style

        image, strip, x, ratio = self.strip_image(database, chunks, qapp)
        bottom = int((strip.height() - 1) * ratio)
        painted = image.pixelColor(x, bottom).name().lower()
        assert painted == style.CARD_BORDER.lower(), (
            f"the strip's underline is {painted}, not {style.CARD_BORDER}")

    def test_the_strip_does_not_wear_the_card_s_paper(self, qapp, database, chunks):
        from studyapp.ui import style

        image, strip, x, ratio = self.strip_image(database, chunks, qapp)
        middle = int((strip.height() // 2) * ratio)
        ground = image.pixelColor(x, middle).name().lower()
        assert ground != style.CARD.lower(), \
            "the strip is the same paper as the table, so it reads as a data row"
        assert ground == style.FILTER_GROUND.lower()


class TestExportAndImportFromTheBank:
    """The buttons that move a bank between machines.

    The dialogs are stubbed; what is under test is what happens around them --
    that the picker decides what travels, that a bad file is refused with a
    sentence rather than a traceback, and that the view catches up afterwards.
    """

    def test_they_sit_with_the_picker_they_act_on(self, qapp, database, chunks):
        # Not with Edit and Retire along the bottom: those act on the selected
        # rows, these act on whichever document the picker is showing.
        repo.insert_questions(database, chunks[0], [mcq()])
        view = BankView(database)
        view.resize(1200, 600)
        view.show()
        qapp.processEvents()

        top = view.documents.y()
        bottom = view.edit_button.y()
        for button in (view.export_button, view.import_button):
            assert abs(button.y() - top) < abs(button.y() - bottom),                 "the button drifted down to the row-level actions"

    def test_export_writes_what_the_picker_is_showing(
            self, qapp, database, chunks, tmp_path, monkeypatch):
        from studyapp.db import transfer

        repo.insert_questions(database, chunks[0], [mcq()])
        other_id, other_chunk = second_document(database)
        repo.insert_questions(database, other_chunk, [mcq(question="Z druhé knihy")])

        view = BankView(database)
        view.documents.setCurrentIndex(view.documents.findData(other_id))

        out = tmp_path / "one-book.json"
        monkeypatch.setattr("studyapp.ui.bank_view.QFileDialog.getSaveFileName",
                            lambda *a, **kw: (str(out), ""))
        view._export()

        payload = transfer.read(out)
        assert transfer.describe(payload)["questions"] == 1
        assert transfer.describe(payload)["filenames"] == ["c.pdf"]

    def test_export_with_all_documents_takes_everything(
            self, qapp, database, chunks, tmp_path, monkeypatch):
        from studyapp.db import transfer

        repo.insert_questions(database, chunks[0], [mcq()])
        _, other_chunk = second_document(database)
        repo.insert_questions(database, other_chunk, [mcq(question="Z druhé knihy")])

        view = BankView(database)
        assert view.document_id is None
        out = tmp_path / "all.json"
        monkeypatch.setattr("studyapp.ui.bank_view.QFileDialog.getSaveFileName",
                            lambda *a, **kw: (str(out), ""))
        view._export()
        assert transfer.describe(transfer.read(out))["questions"] == 2

    def test_cancelling_the_save_dialog_writes_nothing(
            self, qapp, database, chunks, tmp_path, monkeypatch):
        repo.insert_questions(database, chunks[0], [mcq()])
        view = BankView(database)
        monkeypatch.setattr("studyapp.ui.bank_view.QFileDialog.getSaveFileName",
                            lambda *a, **kw: ("", ""))
        view._export()
        assert not list(tmp_path.glob("*.json"))

    def test_import_adds_the_questions_and_refreshes(
            self, qapp, database, chunks, tmp_path, monkeypatch):
        from studyapp.db import transfer

        repo.insert_questions(database, chunks[0], [mcq()])
        source = tmp_path / "export.json"
        transfer.write(database, source, None)

        # A fresh bank to import into.
        target_path = tmp_path / "target.db"
        db_connection.ensure_schema(target_path)
        target = db_connection.connect(target_path)
        try:
            view = BankView(target)
            assert view.model.rowCount() == 0
            heard = []
            view.changed.connect(lambda: heard.append(True))

            monkeypatch.setattr("studyapp.ui.bank_view.QFileDialog.getOpenFileName",
                                lambda *a, **kw: (str(source), ""))
            monkeypatch.setattr(QMessageBox, "question",
                                lambda *a, **kw: QMessageBox.Yes)
            monkeypatch.setattr(QMessageBox, "information", lambda *a, **kw: None)
            view._import()

            assert view.model.rowCount() == 1, "the table did not catch up"
            assert heard, "the other tabs were not told"
            assert view.documents.count() >= 1
        finally:
            target.close()

    def test_declining_the_confirmation_imports_nothing(
            self, qapp, database, chunks, tmp_path, monkeypatch):
        from studyapp.db import transfer

        repo.insert_questions(database, chunks[0], [mcq()])
        source = tmp_path / "export.json"
        transfer.write(database, source, None)

        target_path = tmp_path / "target.db"
        db_connection.ensure_schema(target_path)
        target = db_connection.connect(target_path)
        try:
            view = BankView(target)
            monkeypatch.setattr("studyapp.ui.bank_view.QFileDialog.getOpenFileName",
                                lambda *a, **kw: (str(source), ""))
            monkeypatch.setattr(QMessageBox, "question",
                                lambda *a, **kw: QMessageBox.No)
            view._import()
            assert repo.count_questions(target, active_only=False) == 0
        finally:
            target.close()

    def test_a_file_that_is_not_an_export_is_refused_with_a_sentence(
            self, qapp, database, chunks, tmp_path, monkeypatch):
        repo.insert_questions(database, chunks[0], [mcq()])
        rubbish = tmp_path / "holiday.json"
        rubbish.write_text('{"photos": []}', encoding="utf-8")

        view = BankView(database)
        said = []
        monkeypatch.setattr("studyapp.ui.bank_view.QFileDialog.getOpenFileName",
                            lambda *a, **kw: (str(rubbish), ""))
        monkeypatch.setattr(QMessageBox, "warning",
                            lambda parent, title, text, *a, **kw: said.append(text))
        view._import()

        assert said and "not a StudyApp export" in said[0]
        assert repo.count_questions(database) == 1, "the bank was left alone"


class TestTheSelectionSurvivesAnAction:
    """Starring a row used to clear the selection.

    `refresh()` resets the model and a model reset drops the selection, so every
    action along the bottom row undid the choice that led to it -- select, star,
    and now select again to do the next thing.
    """

    def ready(self, database, chunks, qapp):
        repo.insert_questions(database, chunks[0], [
            mcq(question="První"), mcq(question="Druhá"), mcq(question="Třetí")])
        view = BankView(database)
        view.resize(1100, 500)
        view.show()
        qapp.processEvents()
        return view

    def select(self, view, rows):
        selection = view.table.selectionModel()
        selection.clearSelection()
        for row in rows:
            selection.select(
                view.model.index(row, 0),
                QItemSelectionModel.Select | QItemSelectionModel.Rows)

    def test_starring_keeps_the_row_selected(self, qapp, database, chunks):
        view = self.ready(database, chunks, qapp)
        self.select(view, [1])
        before = view.model.ids_at(view._selected_rows())
        view._toggle_star()
        assert view.model.ids_at(view._selected_rows()) == before

    def test_a_multiple_selection_survives_too(self, qapp, database, chunks):
        view = self.ready(database, chunks, qapp)
        self.select(view, [0, 2])
        before = set(view.model.ids_at(view._selected_rows()))
        view._toggle_star()
        assert set(view.model.ids_at(view._selected_rows())) == before

    def test_retiring_keeps_it_too(self, qapp, database, chunks):
        # Retired questions leave the "Active" filter, so this one is expected
        # to lose them -- what must not happen is losing the others as well.
        view = self.ready(database, chunks, qapp)
        self.select(view, [0, 1])
        view.state.setCurrentIndex(view.state.findData("all"))
        self.select(view, [0, 1])
        kept = set(view.model.ids_at(view._selected_rows()))
        view._set_active(False)
        assert set(view.model.ids_at(view._selected_rows())) == kept

    def test_a_row_the_filter_hides_is_simply_dropped(self, qapp, database, chunks):
        view = self.ready(database, chunks, qapp)
        self.select(view, [0])
        view._set_active(False)          # leaves the Active filter
        assert view._selected_rows() == []
        assert view.model.rowCount() == 2


class TestRemovingADocument:
    def test_it_is_offered_only_for_one_chosen_document(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        second_document(database)
        view = BankView(database)
        assert view.document_id is None
        assert not view.remove_button.isEnabled(), '"All documents" says nothing'

        other = repo.list_documents(database)[0]["id"]
        view.documents.setCurrentIndex(view.documents.findData(other))
        assert view.remove_button.isEnabled()

    def test_confirming_deletes_the_document_and_its_questions(
            self, qapp, database, chunks, monkeypatch):
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        other_id, other_chunk = second_document(database)
        repo.insert_questions(database, other_chunk, [mcq(question="Z druhé knihy")])

        view = BankView(database)
        view.documents.setCurrentIndex(view.documents.findData(other_id))
        monkeypatch.setattr(QMessageBox, "exec", lambda self: None)
        monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: next(
            b for b in self.buttons()
            if self.buttonRole(b) == QMessageBox.DestructiveRole))
        view._remove_document()

        assert repo.get_document(database, other_id) is None
        assert not [r for r in repo.search_questions(database, state="all")
                    if r["question"] == "Z druhé knihy"]
        assert repo.count_questions(database) == 2, "the other book is untouched"

    def test_cancelling_removes_nothing(self, qapp, database, chunks, monkeypatch):
        repo.insert_questions(database, chunks[0], [mcq()])
        view = BankView(database)
        document_id = view.document_id
        monkeypatch.setattr(QMessageBox, "exec", lambda self: None)
        # Anything that is not the Remove button counts as a refusal.
        monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: next(
            b for b in self.buttons()
            if self.buttonRole(b) == QMessageBox.RejectRole))
        view._remove_document()
        assert repo.get_document(database, document_id) is not None
        assert repo.count_questions(database) == 1

    def test_the_warning_counts_what_goes_and_spares_the_file(
            self, qapp, database, chunks, monkeypatch):
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        first = repo.search_questions(database)[0]
        repo.record_attempt(database, None, first["id"], is_correct=True)

        view = BankView(database)
        shown = []
        monkeypatch.setattr(QMessageBox, "setText",
                            lambda self, text: shown.append(text))
        monkeypatch.setattr(QMessageBox, "exec", lambda self: None)
        monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: None)
        view._remove_document()

        assert shown, "no confirmation was shown"
        text = shown[0]
        assert "2 questions" in text and "1 recorded answers" in text
        # "Remove the PDF" is the obvious reading of the button and the wrong one.
        assert "PDF file itself is left alone" in text


class TestTheStarColumnIsLabelled:
    def test_the_heading_says_star(self, qapp, database):
        model = QuestionTableModel(database)
        assert model.headerData(QuestionTableModel.STAR, Qt.Horizontal,
                                Qt.DisplayRole) == "Star"

    def test_the_heading_is_not_truncated(self, qapp, database, chunks):
        # Measured by Qt in whatever font is in force, rather than against a
        # constant here -- the test font and the app's are not the same, and a
        # number that passes under one can clip under the other.
        repo.insert_questions(database, chunks[0], [mcq()])
        view = BankView(database)
        view.resize(1100, 500)
        view.show()
        qapp.processEvents()
        header = view.table.horizontalHeader()
        column = QuestionTableModel.STAR
        assert header.sectionSize(column) >= header.sectionSizeHint(column)

    def test_the_star_box_still_fits_beneath_it(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        view = BankView(database)
        view.resize(1100, 500)
        view.show()
        qapp.processEvents()
        assert view.starred_only.isVisible()
        assert view.starred_only.width() >= view.starred_only.sizeHint().width()


class TestEditingDoesNotMoveTheAnswer:
    """The radio button's id is a position among four fields; the saved list has
    the blank ones taken out. Carrying the id across unchanged moved the right
    answer, silently, and every attempt after that was graded against it.
    """

    def dialog(self, database, chunks, correct_index=1):
        repo.insert_questions(database, chunks[0], [mcq(
            options=["A", "B", "C", "D"], correct_index=correct_index)])
        return EditDialog(repo.search_questions(database)[0])

    def saved(self, dialog):
        changes = dialog.changes()
        return json.loads(changes["options_json"]), changes["correct_index"]

    def test_clearing_an_option_before_the_answer(self, qapp, database, chunks):
        # B is correct; clear A. The answer used to come out as C.
        dialog = self.dialog(database, chunks, correct_index=1)
        dialog.options[0].setText("")
        options, index = self.saved(dialog)
        assert options == ["B", "C", "D"]
        assert options[index] == "B"

    def test_clearing_two_options_before_the_answer(self, qapp, database, chunks):
        dialog = self.dialog(database, chunks, correct_index=3)
        dialog.options[0].setText("")
        dialog.options[1].setText("")
        options, index = self.saved(dialog)
        assert options == ["C", "D"]
        assert options[index] == "D"

    def test_clearing_an_option_after_the_answer(self, qapp, database, chunks):
        dialog = self.dialog(database, chunks, correct_index=1)
        dialog.options[3].setText("")
        options, index = self.saved(dialog)
        assert options == ["A", "B", "C"]
        assert options[index] == "B"

    def test_clearing_the_answer_itself_is_refused(self, qapp, database, chunks):
        dialog = self.dialog(database, chunks, correct_index=1)
        dialog.options[1].setText("")
        assert dialog.problem() == "The correct option is empty."

    def test_clearing_a_later_option_while_the_last_is_correct_is_allowed(
            self, qapp, database, chunks):
        # Rejected before, because position 3 was past the end of the shortened
        # list -- even though the marked field was perfectly full.
        dialog = self.dialog(database, chunks, correct_index=3)
        dialog.options[1].setText("")
        assert dialog.problem() is None
        options, index = self.saved(dialog)
        assert options[index] == "D"

    def test_changing_the_text_of_the_answer_keeps_it_marked(
            self, qapp, database, chunks):
        dialog = self.dialog(database, chunks, correct_index=1)
        dialog.options[1].setText("B, opraveno")
        options, index = self.saved(dialog)
        assert options[index] == "B, opraveno"

    def test_the_saved_answer_survives_a_round_trip(self, qapp, database, chunks):
        dialog = self.dialog(database, chunks, correct_index=1)
        dialog.options[0].setText("")
        question_id = repo.search_questions(database)[0]["id"]
        repo.update_question(database, question_id, **dialog.changes())

        row = repo.get_question(database, question_id)
        options = json.loads(row["options_json"])
        assert options[row["correct_index"]] == "B"


class TestProgressPerDocument:
    """Two books used to share one accuracy and one chapter table.

    That made both useless: a chapter sitting at 40% is the whole point of this
    tab, and it cannot be seen through the average of a book that is going fine.
    """

    def two_books(self, database, chunks):
        """The fixture book answered right; a second one answered wrong."""
        repo.insert_questions(database, chunks[0], [mcq(), mcq()])
        other_id, other_chunk = second_document(database, title="9 Paměť")
        repo.insert_questions(database, other_chunk, [
            mcq(question="Z druhé knihy A"), mcq(question="Z druhé knihy B")])

        first = [r["id"] for r in repo.search_questions(database)
                 if not r["question"].startswith("Z druhé")]
        second = [r["id"] for r in repo.search_questions(database)
                  if r["question"].startswith("Z druhé")]
        # Three answers each, so the weakest-chapter line has a sample it will
        # speak about (it ignores anything under five answers).
        for _ in range(3):
            for question_id in first:
                repo.record_attempt(database, None, question_id, is_correct=True)
            for question_id in second:
                repo.record_attempt(database, None, question_id, is_correct=False)
        return other_id

    def test_one_book_does_not_dilute_the_other(self, qapp, database, chunks):
        other_id = self.two_books(database, chunks)
        view = StatsView(database)

        view.refresh()
        assert view.counters["accuracy"].value.text() == "50%", "both books together"

        view.documents.setCurrentIndex(view.documents.findData(other_id))
        assert view.counters["accuracy"].value.text() == "0%"

    def test_the_chapter_table_follows_the_document(self, qapp, database, chunks):
        other_id = self.two_books(database, chunks)
        view = StatsView(database)
        view.refresh()
        everything = view.chapters.rowCount()

        view.documents.setCurrentIndex(view.documents.findData(other_id))
        assert view.chapters.rowCount() < everything
        assert view.chapters.rowCount() == 1

    def test_the_counters_follow_too(self, qapp, database, chunks):
        other_id = self.two_books(database, chunks)
        view = StatsView(database)
        view.documents.setCurrentIndex(view.documents.findData(other_id))

        assert view.counters["bank"].value.text() == "2"
        assert view.counters["practised"].value.text() == "2"
        assert view.counters["mistakes"].value.text() == "2"
        assert view.counters["attempts"].value.text() == "6"

    def test_the_weakest_chapter_is_the_one_in_this_book(
            self, qapp, database, chunks):
        other_id = self.two_books(database, chunks)
        view = StatsView(database)
        view.documents.setCurrentIndex(view.documents.findData(other_id))
        assert "9 Paměť" in view.weakest.text()

    def test_session_history_is_scoped(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        _, other_chunk = second_document(database)
        repo.insert_questions(database, other_chunk, [mcq(question="Cizí")])

        mine = repo.start_session(database, ("mcq",), "all")
        repo.record_attempt(database, mine,
                            repo.search_questions(database)[0]["id"], is_correct=True)
        theirs = repo.start_session(database, ("mcq",), "all")
        repo.record_attempt(
            database, theirs,
            [r["id"] for r in repo.search_questions(database)
             if r["question"] == "Cizí"][0], is_correct=False)

        assert len(repo.session_history(database)) == 2
        assert len(repo.session_history(
            database, document_id=repo.list_documents(database)[-1]["id"])) == 1

    def test_a_single_book_bank_shows_no_picker(self, qapp, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])
        view = StatsView(database)
        view.refresh()
        assert view.documents.isHidden()
        assert view.document_id is None

    def test_all_documents_is_the_default(self, qapp, database, chunks):
        self.two_books(database, chunks)
        view = StatsView(database)
        view.refresh()
        assert view.documents.itemData(0) is None
        assert view.document_id is None


class TestASingleDocumentBankCanStillBeManaged:
    """The bank picks its one document; Quiz and Progress pick nothing.

    That looks like three implementations drifting apart, and it is deliberate:
    `Remove…` and `Locate PDF…` act on a named document and are disabled without
    one. Unify this and a bank holding one book cannot delete it.
    """

    def one_book(self, database, chunks):
        repo.insert_questions(database, chunks[0], [mcq()])

    def test_the_bank_names_its_only_document(self, qapp, database, chunks):
        self.one_book(database, chunks)
        view = BankView(database)
        assert view.document_id == repo.list_documents(database)[0]["id"]

    def test_and_so_it_can_be_removed(self, qapp, database, chunks):
        self.one_book(database, chunks)
        view = BankView(database)
        assert view.remove_button.isEnabled(), \
            "the only book in the bank cannot be deleted"

    def test_the_quiz_picks_nothing_and_hides_the_row(self, qapp, database, chunks):
        from studyapp.ui.quiz_view import QuizView

        self.one_book(database, chunks)
        view = QuizView(database)
        view.start_screen.refresh()
        assert view.start_screen.document.currentData() is None
        assert view.start_screen.document.isHidden()

    def test_progress_does_the_same(self, qapp, database, chunks):
        self.one_book(database, chunks)
        view = StatsView(database)
        view.refresh()
        assert view.document_id is None
        assert view.documents.isHidden()

    def test_all_three_show_the_same_questions_anyway(self, qapp, database, chunks):
        # One document and "all documents" are the same set, so the figures agree
        # however each tab got there.
        self.one_book(database, chunks)
        bank = BankView(database)
        stats = StatsView(database)
        stats.refresh()
        assert bank.model.rowCount() == int(stats.counters["bank"].value.text())
