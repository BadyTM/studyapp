"""The practice view.

Most of these check one thing: that nothing visible before an answer gives the
answer away. Blurring the page is the obvious half of that and the easy half --
the page number and the chapter title in the strip above it would answer plenty
of questions on their own, and they are the part a refactor quietly breaks.
"""

import pymupdf
import pytest
from PySide6.QtCore import Qt

from studyapp.db import connection as db_connection
from studyapp.db import repo
from studyapp.quiz.session import Outcome, Spec
from studyapp.ui import style
from studyapp.ui.pdf_view import SourcePane
from studyapp.ui.quiz_view import QuizView
from studyapp.ui.session_header import MAX_DOTS, SessionHeader


@pytest.fixture
def book(tmp_path) -> str:
    """A real PDF on disk.

    The pane refuses to reveal a document it cannot open -- it says the file has
    moved instead of showing a blank page -- so these tests need one that exists.
    """
    path = tmp_path / "book.pdf"
    document = pymupdf.open()
    for _ in range(50):
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(60, 60, 400, 700),
                            "Trvalost znamená, že změny zůstávají zachovány. " * 8,
                            fontsize=11, fontname="helv")
    document.save(path)
    document.close()
    return str(path)


@pytest.fixture
def database(tmp_path, book):
    path = tmp_path / "quiz.db"
    db_connection.ensure_schema(path)
    connection = db_connection.connect(path)
    document_id = repo.insert_document(connection, book,
                                       "book.pdf", "sha-book", 50, "cs")
    connection.execute(
        """INSERT INTO chunks (document_id, ordinal, page_start, page_end, title,
                               text, sha256, status, updated_at)
           VALUES (?, 0, 40, 45, '6.2 Vlastnosti transakcí', 'text', 'sha', 'done', ?)""",
        (document_id, repo.utcnow()))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def chunk_id(database):
    return database.execute("SELECT id FROM chunks").fetchone()["id"]


def mcq(**overrides):
    base = {
        "type": "mcq",
        "question": "Která vlastnost zaručuje trvalost změn?",
        "options": ["Atomicita", "Konzistence", "Izolace", "Trvalost"],
        "correct_index": 3,
        "explanation": "Trvalost se týká přežití změn po pádu systému.",
        "source_quote": "Trvalost znamená, že změny zůstávají zachovány.",
        "quote_score": 1.0,
        "page": 43,
        "rects": [[72.0, 100.0, 400.0, 114.0]],
    }
    base.update(overrides)
    return base


def open_question(**overrides):
    return mcq(type="open", options=None, correct_index=None,
               correct_text="Zápisem žurnálu na trvalé médium.", **overrides)


@pytest.fixture
def view(qapp, database):
    widget = QuizView(database)
    yield widget
    widget.deleteLater()


@pytest.fixture
def started(view, database, chunk_id):
    """A view with a session already running."""
    def start(questions, spec=None):
        repo.insert_questions(database, chunk_id, questions)
        view.begin_session(spec or Spec(limit=len(questions)))
        return view
    return start


class TestNothingLeaksBeforeAnswering:
    def test_the_pane_starts_locked(self, started):
        view = started([mcq()])
        assert view.source.stack.currentIndex() == 0
        assert not view.source.is_revealed

    def test_the_page_number_is_hidden_until_the_answer_is_in(self, started):
        # "p. 44" beside a question about durability answers it by itself.
        view = started([mcq()])
        assert view.source.page_label.text() == "locked"
        assert "44" not in view.source.page_label.text()

    def test_the_chapter_title_is_hidden_too(self, started):
        # "6.2 Vlastnosti transakcí" is as much of a giveaway as the page.
        view = started([mcq()])
        assert view.source.chapter_label.text().strip() == ""

    def test_both_appear_on_reveal(self, started):
        view = started([mcq()])
        view._on_submit(3)
        assert "44" in view.source.page_label.text()
        assert view.source.chapter_label.text() == "6.2 Vlastnosti transakcí"
        assert view.source.is_revealed

    def test_the_next_question_locks_it_again(self, started):
        view = started([mcq(), mcq(question="Druhá otázka?")])
        view._on_submit(3)
        assert view.source.is_revealed
        view._on_advance()
        assert not view.source.is_revealed
        assert view.source.page_label.text() == "locked"

    def test_an_unlocated_quote_says_so_rather_than_guessing(self, started):
        view = started([mcq(page=None, rects=None)])
        view._on_submit(3)
        assert "not located" in view.source.page_label.text()


class TestMultipleChoice:
    def test_submit_is_disabled_until_something_is_selected(self, started):
        view = started([mcq()])
        assert not view.card.primary.isEnabled()
        view.card.select(1)
        assert view.card.primary.isEnabled()

    def test_selecting_marks_only_that_option(self, started):
        view = started([mcq()])
        view.card.select(2)
        states = [button.property("state") for button in view.card.options]
        assert states == ["", "", "selected", ""]

    def test_the_result_marks_right_wrong_and_the_rest(self, started):
        view = started([mcq()])
        view._on_submit(0)
        states = [button.property("state") for button in view.card.options]
        assert states[3] == "correct"
        assert states[0] == "wrong"
        assert states[1] == states[2] == "dimmed"

    def test_options_stop_accepting_clicks_after_the_answer(self, started):
        view = started([mcq()])
        view._on_submit(1)
        assert all(not button.isEnabled() for button in view.card.options[:4])

    def test_the_explanation_appears_with_the_result(self, started):
        view = started([mcq()])
        assert view.card.answer.isHidden()
        view._on_submit(3)
        assert "přežití změn" in view.card.answer.text()

    def test_the_button_becomes_next(self, started):
        view = started([mcq()])
        view._on_submit(3)
        assert view.card.primary.text() == "Next"

    def test_options_never_take_focus(self, started):
        # If an option had focus, Space would press it instead of revealing.
        view = started([mcq()])
        assert all(button.focusPolicy() == Qt.NoFocus for button in view.card.options)

    def test_a_three_option_question_hides_the_fourth_row(self, started):
        # Validation rejects these, but a hand-edited question could still arrive.
        view = started([mcq(options=["A", "B", "C"], correct_index=1)])
        assert view.card.options[3].isHidden()
        assert not view.card.options[2].isHidden()


class TestOptionsWrap:
    """Long options must wrap rather than widen the pane.

    A QPushButton will not wrap: its size hint is the whole option on one line,
    and a splitter honours the minimum width of what it holds, so a single long
    distractor squeezed the source pane down to a strip. Distractors are usually
    a full clause, so that was most questions.
    """

    LONG = ("In the absolute period no action potential can be generated because "
            "many voltage-gated sodium channels are inactivated; in the relative "
            "period one is possible but needs a stronger stimulus")

    def test_the_option_label_wraps(self, started):
        view = started([mcq()])
        assert view.card.options[0].label.wordWrap()

    def test_a_long_option_does_not_widen_the_card(self, started):
        view = started([mcq(options=[self.LONG, "b", "c", "d"], correct_index=0)])
        # The option must not demand its full single-line width, which for this
        # text is well over a thousand pixels.
        assert view.card.options[0].minimumSizeHint().width() < 400

    def test_the_number_and_the_text_are_separate(self, started):
        # The number is set in a mono face beside the text, not glued onto the
        # front of it, so wrapped lines align under the first word.
        view = started([mcq()])
        button = view.card.options[0]
        assert button.number.text() == "1"
        assert button.label.text() == "Atomicita"
        assert "1" not in button.label.text()

    def test_clicking_an_option_selects_it(self, started):
        view = started([mcq()])
        view.card.options[2].clicked.emit()
        assert view.card.options[2].property("state") == "selected"

    def test_a_disabled_option_ignores_a_click(self, started):
        view = started([mcq()])
        view._on_submit(1)
        before = [b.property("state") for b in view.card.options]
        view.card.options[0].clicked.emit()
        assert [b.property("state") for b in view.card.options] == before

    def test_the_result_colours_reach_the_label(self, started):
        # The text colour lives on the child label; a parent restyle does not
        # reach it on its own.
        view = started([mcq()])
        view._on_submit(0)
        assert view.card.options[0].label.property("state") == "wrong"
        assert view.card.options[1].label.property("state") == "dimmed"


class TestSplit:
    def test_the_panes_start_equal(self, view):
        view.resize(1400, 900)
        sizes = view.splitter.sizes()
        assert abs(sizes[0] - sizes[1]) <= 2, "the split should open at 50/50"

    def test_neither_pane_can_be_collapsed_away(self, view):
        assert not view.splitter.childrenCollapsible()
        assert view.left.minimumWidth() > 0
        assert view.source.minimumWidth() > 0


class TestOpenQuestions:
    def test_no_options_are_shown(self, started):
        view = started([open_question()])
        assert all(button.isHidden() for button in view.card.options)

    def test_the_button_offers_reveal(self, started):
        view = started([open_question()])
        assert view.card.primary.text() == "Reveal answer"
        assert view.card.primary.isEnabled()

    def test_grading_appears_only_after_revealing(self, started):
        view = started([open_question()])
        assert view.card.yes.isHidden()
        view._on_reveal()
        assert not view.card.yes.isHidden() and not view.card.no.isHidden()

    def test_the_model_answer_comes_with_the_reveal(self, started):
        view = started([open_question()])
        view._on_reveal()
        assert "žurnálu" in view.card.answer.text()

    def test_grading_replaces_the_buttons_with_next(self, started):
        view = started([open_question()])
        view._on_reveal()
        view._on_grade(True)
        assert view.card.yes.isHidden()
        assert view.card.primary.text() == "Next"

    def test_grading_without_revealing_does_nothing(self, started, database):
        view = started([open_question()])
        view._on_grade(True)
        assert database.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


class TestKeyboardRules:
    def test_space_does_not_submit_a_multiple_choice_question(self, started, database):
        # A stray Space would otherwise become a recorded wrong answer.
        view = started([mcq()])
        view.card.select(0)
        view._on_space()
        assert database.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert not view.source.is_revealed

    def test_space_reveals_an_open_question(self, started):
        view = started([open_question()])
        view._on_space()
        assert view.source.is_revealed

    def test_next_does_nothing_until_the_question_is_answered(self, started):
        view = started([mcq(), mcq(question="Druhá?")])
        view._on_advance()
        assert view.session.index == 0, "Reveal is the only way forward"

    def test_y_and_n_are_ignored_on_a_multiple_choice_question(self, started, database):
        view = started([mcq()])
        view._on_grade(True)
        assert database.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


class TestStarring:
    def test_the_button_follows_the_state(self, started):
        view = started([mcq()])
        assert view.card.star.text() == "☆ Star"
        view._on_star()
        assert view.card.star.text() == "★ Starred"


class TestSessionFlow:
    def test_the_summary_appears_at_the_end(self, started):
        view = started([mcq()])
        view._on_submit(3)
        view._on_advance()
        assert view.left.currentIndex() == 2
        assert view.summary.score.text() == "1 / 1"

    def test_an_empty_source_says_so_instead_of_starting(self, view):
        view.begin_session(Spec(source="mistakes"))
        assert view.session is None
        assert "Nothing matches" in view.start_screen.bank.text()

    def test_the_start_screen_counts_the_bank(self, view, database, chunk_id):
        repo.insert_questions(database, chunk_id, [mcq(), mcq()])
        view.start_screen.refresh()
        assert "2 questions" in view.start_screen.bank.text()

    def test_mistakes_are_not_offered_when_there_are_none(self, view, database, chunk_id):
        repo.insert_questions(database, chunk_id, [mcq()])
        view.start_screen.refresh()
        sources = [view.start_screen.source.itemData(i)
                   for i in range(view.start_screen.source.count())]
        assert "mistakes" not in sources


class TestSessionHeader:
    def test_the_current_mark_is_neutral_before_it_is_answered(self, qapp):
        # Never pre-coloured: this is the one strip the reader looks at while
        # still thinking about the question.
        header = SessionHeader()
        header.set_session([Outcome.UNANSWERED] * 5, 2, "whole bank")
        assert header._dot_color(2, Outcome.UNANSWERED) == style.DOT_CURRENT
        assert header._dot_color(3, Outcome.UNANSWERED) == style.DOT_FUTURE

    def test_results_colour_the_marks_behind(self, qapp):
        header = SessionHeader()
        assert header._dot_color(0, Outcome.CORRECT) == style.DOT_CORRECT
        assert header._dot_color(0, Outcome.WRONG) == style.DOT_WRONG

    def test_the_label_reads_like_the_design(self, qapp):
        header = SessionHeader()
        header.set_session([Outcome.UNANSWERED] * 20, 6, "mistakes only")
        assert header._right_text() == "7 / 20 · mistakes only"

    def test_a_long_session_falls_back_to_a_bar(self, qapp):
        # Past ~30 the marks no longer fit, and shrinking them makes them unreadable.
        header = SessionHeader()
        header.set_session([Outcome.UNANSWERED] * (MAX_DOTS + 30), 0, "whole bank")
        assert len(header._outcomes) > MAX_DOTS

    def test_no_session_is_harmless(self, qapp):
        header = SessionHeader()
        header.set_session([], 0, "ready")
        assert header._right_text() == "ready"


class TestHighlightOverlay:
    """The marker over the quote.

    Qt paints search results in a hardcoded cyan that no palette or property
    changes, and which is unreadable over black text. The marker is drawn here
    instead, from the rectangles that generation already worked out.
    """

    def test_it_draws_the_stored_rectangles(self, qapp):
        pane = SourcePane()
        pane.reveal(43, "quote", "ch", 200, [[72.0, 100.0, 400.0, 114.0]])
        assert len(pane.marks.marker_rects()) == 1
        assert pane.highlight_found()

    def test_a_question_with_no_rectangles_draws_nothing(self, qapp):
        pane = SourcePane()
        pane.reveal(43, "quote", "ch", 200, [])
        assert pane.marks.marker_rects() == []
        assert not pane.highlight_found()

    def test_locking_clears_the_marker(self, qapp):
        # Otherwise the marker would sit over the next question's blurred page,
        # pointing at the answer.
        pane = SourcePane()
        pane.reveal(43, "quote", "ch", 200, [[72.0, 100.0, 400.0, 114.0]])
        pane.lock()
        assert pane.marks.marker_rects() == []

    def test_the_marker_is_wider_than_the_glyphs(self, qapp):
        # A marker drawn exactly on the glyph boxes reads as a filled box.
        pane = SourcePane()
        pane.reveal(0, "quote", "ch", 200, [[72.0, 100.0, 172.0, 114.0]])
        rect = pane.marks.marker_rects()[0]
        scale = pane.marks.scale()
        assert rect.width() > 100 * scale
        assert rect.height() > 14 * scale

    def test_the_transform_follows_the_scroll(self, qapp):
        # Tested as arithmetic rather than by scrolling a real view: without a
        # document loaded the scrollbar has no range, so setValue does nothing
        # and the test would pass while proving none of this.
        pane = SourcePane()
        pane.marks.set_marks(0, [[72.0, 400.0, 400.0, 414.0]])
        at_top = pane.marks.viewport_rect([72.0, 400.0, 400.0, 414.0], 1.0, 0.0)

        pane.viewer.verticalScrollBar().setRange(0, 1000)
        pane.viewer.verticalScrollBar().setValue(50)
        scrolled = pane.marks.viewport_rect([72.0, 400.0, 400.0, 414.0], 1.0, 0.0)
        assert scrolled.top() == pytest.approx(at_top.top() - 50, abs=0.01)

    def test_the_page_offset_stacks_the_pages_above(self, qapp):
        # A rectangle on page 5 sits below everything before it, or the marker
        # would land on page one every time.
        pane = SourcePane()
        pane.marks.set_marks(0, [[0.0, 0.0, 10.0, 10.0]])
        first = pane.marks.page_top(1.0)
        pane.marks.set_marks(5, [[0.0, 0.0, 10.0, 10.0]])
        assert pane.marks.page_top(1.0) > first

    def test_nothing_to_scroll_to_is_harmless(self, qapp):
        pane = SourcePane()
        pane.marks.clear()
        pane.marks.scroll_to_marks()


class TestThePreviewArrivingDuringASession:
    """The blurred pages may not be on disk yet when a session starts.

    Briefly for a book imported here. For the whole session on a bank that came
    from someone else: it travels without the cache, so the render only starts
    once the reader points the document at its PDF, and until it finishes the
    pane says "preview not ready" at every card.

    Each test takes the cached page away first. The data directory is shared by
    the whole run and the hash is the same in every test here, so whichever of
    these ran first would otherwise leave the others looking at a page that was
    already there -- which is the trap `conftest.py` is written to avoid.
    """

    SHA = "sha-book"

    def _waiting(self, started):
        """A session whose pane has no page to show."""
        from studyapp.ingest import blur

        blur.page_path(self.SHA, 0).unlink(missing_ok=True)
        view = started([mcq()])
        assert view.source.blurred.text() == "preview not ready",             "the pane was not waiting for anything to begin with"
        return view

    def _render_resting_page(self, book):
        """Just the page the pane is resting on. Rendering 50 takes seconds."""
        from studyapp.ingest import blur

        with pymupdf.open(book) as document:
            blur.write_page(blur.page_path(self.SHA, 0),
                            blur.blur_pixmap(document[0]))

    def test_the_pane_takes_the_page_up_without_advancing(self, started, book):
        view = self._waiting(started)

        self._render_resting_page(book)
        view.preview_ready(self.SHA)

        assert view.source.blurred.text() == "",             "the pane was still waiting for a page that had been rendered"

    def test_a_different_book_finishing_is_ignored(self, started, book):
        # This book's page is on disk, so a pane that redraws will find it.
        # Without that the test cannot tell a pane that ignored the signal
        # from one that acted on it and found nothing: both say the same.
        view = self._waiting(started)
        self._render_resting_page(book)

        view.preview_ready("sha-some-other-book")

        assert view.source.blurred.text() == "preview not ready",             "another book's render redrew this one's pane"

    def test_a_revealed_pane_stays_revealed(self, started, book):
        view = self._waiting(started)
        view._on_submit(3)
        assert view.source.is_revealed

        self._render_resting_page(book)
        view.preview_ready(self.SHA)

        assert view.source.is_revealed, "the answer re-blurred itself"

    def test_it_is_harmless_with_no_session(self, view):
        view.preview_ready(self.SHA)


class TestTheDocumentMovesMidSession:
    """A session loads each question's path when it starts.

    Relocating the PDF on the Generate tab therefore leaves an open session
    holding the old one: the pane says the file is missing while the database
    already knows where it is.
    """

    @pytest.fixture
    def lost(self, database, tmp_path, book):
        """A database whose recorded path is wrong, as a shared one arrives.

        The path is broken before the session starts rather than during it: the
        viewer holds the file open once loaded, so Windows will not let the test
        move it out from under itself -- and this is the real sequence anyway.
        """
        document_id = repo.list_documents(database)[0]["id"]
        database.execute("UPDATE documents SET path = ? WHERE id = ?",
                         (str(tmp_path / "not here" / "book.pdf"), document_id))
        database.commit()
        return document_id

    def _found_again(self, database, document_id, book):
        database.execute("UPDATE documents SET path = ? WHERE id = ?",
                         (book, document_id))
        database.commit()

    def test_a_running_session_takes_up_the_new_path(self, started, database, lost, book):
        view = started([mcq()])
        assert view.session.current.pdf_path != book

        self._found_again(database, lost, book)
        view.document_moved()
        assert view.session.current.pdf_path == book

    def test_the_pane_recovers_without_advancing(self, started, database, lost, book):
        # The reader is looking at the "file is missing" message; they should
        # not have to move on to see it fixed.
        view = started([mcq()])
        view._on_submit(3)
        assert "not found" in view.source.page_label.text()

        self._found_again(database, lost, book)
        view.document_moved()
        assert "not found" not in view.source.page_label.text()
        assert view.source.is_revealed

    def test_it_is_harmless_with_no_session(self, view):
        view.document_moved()

    def test_the_path_is_restated_on_every_reveal(self, started, database):
        # Set once at the start of the session, it could go stale silently.
        view = started([mcq(), mcq(question="Druhá?")])
        view._on_submit(3)
        assert view.source._pdf_path == view.session.current.pdf_path


class TestSourcePane:
    def test_a_missing_blur_cache_says_so_rather_than_showing_the_page(self, qapp):
        # Failing open here would show the unblurred page and give everything away.
        pane = SourcePane()
        pane.set_document(None, "sha-that-has-no-cache")
        pane.lock()
        assert pane.stack.currentIndex() == 0
        assert "not ready" in pane.blurred.text()

    def test_reveal_switches_layers(self, qapp):
        pane = SourcePane()
        pane.reveal(43, "Trvalost znamená", "6.2 Vlastnosti", 200)
        assert pane.is_revealed
        assert pane.page_label.text() == "p. 44 / 200"

    def test_a_missing_pdf_is_explained_rather_than_shown_blank(self, qapp, tmp_path):
        # The path is recorded at import; the file can be moved or renamed any
        # time after. Qt loads nothing and says nothing, so the pane went blank
        # while the strip above it still announced a page number.
        pane = SourcePane()
        pane.set_document(str(tmp_path / "gone.pdf"), "sha")
        pane.reveal(43, "quote", "ch", 200, [[10.0, 10.0, 100.0, 24.0]])

        assert "not found" in pane.page_label.text()
        assert "gone.pdf" in pane.blurred.text()
        assert not pane.is_revealed, "an empty viewer is worse than the message"
        assert pane.marks.marker_rects() == []

    def test_a_pane_with_no_document_makes_no_such_claim(self, qapp):
        # Nothing is missing if nothing was ever named.
        pane = SourcePane()
        pane.reveal(43, "quote", "ch", 200)
        assert "not found" not in pane.page_label.text()

    def test_lock_puts_it_back(self, qapp):
        pane = SourcePane()
        pane.reveal(43, "quote", "chapter", 200)
        pane.lock()
        assert not pane.is_revealed
        assert pane.page_label.text() == "locked"

    def test_the_marker_follows_the_revealed_page(self, qapp):
        pane = SourcePane()
        pane.reveal(43, "quote", "ch", 200, [[72.0, 100.0, 400.0, 114.0]])
        assert pane.marks._page == 43


def second_book(connection, tmp_path, title="9 Paměť"):
    """A second document in the same bank, with its own chunk and chapter."""
    path = tmp_path / "other.pdf"
    document = pymupdf.open()
    for _ in range(10):
        document.new_page()
    document.save(path)
    document.close()

    document_id = repo.insert_document(connection, str(path), "other.pdf",
                                       "sha-other", 10, "cs")
    connection.execute(
        """INSERT INTO chunks (document_id, ordinal, page_start, page_end, title,
                               text, sha256, status, updated_at)
           VALUES (?, 0, 0, 5, ?, 'text', 'sha-other-chunk', 'done', ?)""",
        (document_id, title, repo.utcnow()))
    connection.commit()
    chunk_id = connection.execute(
        "SELECT id FROM chunks WHERE document_id = ?", (document_id,)).fetchone()["id"]
    return document_id, chunk_id


class TestChoosingTheDocument:
    """Which book to draw a session from.

    The picker used to not exist, and the start screen took `documents[0]`,
    which meant a second book's questions could not be practised at all.
    """

    def test_one_book_shows_no_picker(self, qapp, database, chunk_id):
        repo.insert_questions(database, chunk_id, [mcq()])
        view = QuizView(database)
        view.start_screen.refresh()
        assert view.start_screen.document.isHidden()
        assert view.start_screen.document_label.isHidden()

    def test_a_second_book_brings_the_picker_out(self, qapp, database, chunk_id,
                                                 tmp_path):
        repo.insert_questions(database, chunk_id, [mcq()])
        _, other_chunk = second_book(database, tmp_path)
        repo.insert_questions(database, other_chunk, [mcq(question="Co je paměť?")])

        view = QuizView(database)
        view.start_screen.refresh()
        assert not view.start_screen.document.isHidden()
        assert view.start_screen.document.itemData(0) is None, "All documents first"
        assert view.start_screen.document.count() == 3

    def test_the_spec_carries_the_chosen_document(self, qapp, database, chunk_id,
                                                  tmp_path):
        repo.insert_questions(database, chunk_id, [mcq()])
        other_id, other_chunk = second_book(database, tmp_path)
        repo.insert_questions(database, other_chunk, [mcq(question="Co je paměť?")])

        view = QuizView(database)
        screen = view.start_screen
        screen.refresh()
        screen.document.setCurrentIndex(screen.document.findData(other_id))

        specs = []
        screen.start.connect(specs.append)
        screen.begin.click()
        assert specs[0].document_id == other_id

    def test_a_session_from_one_book_asks_only_its_questions(
            self, qapp, database, chunk_id, tmp_path):
        repo.insert_questions(database, chunk_id, [mcq(), mcq()])
        other_id, other_chunk = second_book(database, tmp_path)
        repo.insert_questions(database, other_chunk, [mcq(question="Co je paměť?")])

        view = QuizView(database)
        screen = view.start_screen
        screen.refresh()
        screen.document.setCurrentIndex(screen.document.findData(other_id))
        screen.begin.click()

        assert view.session is not None
        assert [q.text for q in view.session.questions] == ["Co je paměť?"]

    def test_the_bank_count_follows_the_chosen_document(
            self, qapp, database, chunk_id, tmp_path):
        repo.insert_questions(database, chunk_id, [mcq(), mcq()])
        other_id, other_chunk = second_book(database, tmp_path)
        repo.insert_questions(database, other_chunk, [mcq(question="Co je paměť?")])

        view = QuizView(database)
        screen = view.start_screen
        screen.refresh()
        screen.document.setCurrentIndex(screen.document.findData(other_id))
        # Promising 3 while a session would draw 1 is the kind of small lie that
        # makes the whole screen untrustworthy.
        assert "1 questions in the bank" in screen.bank.text()

    def test_the_chapter_list_follows_the_chosen_document(
            self, qapp, database, chunk_id, tmp_path):
        repo.insert_questions(database, chunk_id, [mcq()])
        other_id, other_chunk = second_book(database, tmp_path)
        repo.insert_questions(database, other_chunk, [mcq(question="Co je paměť?")])

        view = QuizView(database)
        screen = view.start_screen
        screen.refresh()
        screen.document.setCurrentIndex(screen.document.findData(other_id))
        titles = [screen.chapters.itemData(i) for i in range(screen.chapters.count())]
        assert titles == ["9 Paměť"]


class TestTheBankChangingUnderARunningSession:
    """A session is a list of questions read into memory when it started.

    Delete those questions while it runs -- remove their document, purge retired
    ones -- and it carries on asking them. Answering one then writes an attempt
    against a row that is gone and fails on the foreign key, which under pythonw
    is a traceback nobody sees: a question that will not accept an answer.
    """

    def running(self, database, chunk_id, qapp, count=4):
        repo.insert_questions(database, chunk_id, [
            mcq(question=f"Otázka {n}?") for n in range(count)])
        view = QuizView(database)
        view.start_screen.refresh()
        view.start_screen.begin.click()
        qapp.processEvents()
        assert view.session is not None
        return view

    def test_deleting_its_questions_ends_the_session(self, qapp, database, chunk_id):
        view = self.running(database, chunk_id, qapp)
        document_id = repo.list_documents(database)[0]["id"]
        repo.delete_document(database, document_id)

        view.bank_changed()

        assert view.session is None
        assert "no longer in the bank" in view.start_screen.bank.text()

    def test_an_answer_can_no_longer_be_written_against_a_deleted_row(
            self, qapp, database, chunk_id):
        # The failure this is here to prevent, stated as the thing that used to
        # happen: sqlite3.IntegrityError, FOREIGN KEY constraint failed.
        view = self.running(database, chunk_id, qapp)
        repo.delete_document(database, repo.list_documents(database)[0]["id"])
        view.bank_changed()
        assert view.session is None, "a live session here would raise on answer()"

    def test_a_different_document_leaves_the_session_alone(
            self, qapp, database, chunk_id, tmp_path):
        view = self.running(database, chunk_id, qapp)
        before = view.session

        other_id, other_chunk = second_book(database, tmp_path)
        repo.insert_questions(database, other_chunk, [mcq(question="Cizí")])
        repo.delete_document(database, other_id)

        view.bank_changed()
        assert view.session is before, "an unrelated removal stopped the session"

    def test_starring_does_not_stop_anything(self, qapp, database, chunk_id):
        # `changed` fires for every bank action, most of which delete nothing.
        view = self.running(database, chunk_id, qapp)
        before = view.session
        repo.set_starred(database, view.session.questions[0].id, True)
        view.bank_changed()
        assert view.session is before

    def test_purging_a_retired_question_in_the_session_ends_it_too(
            self, qapp, database, chunk_id):
        view = self.running(database, chunk_id, qapp)
        doomed = view.session.questions[2].id
        repo.set_active(database, [doomed], False)
        repo.purge_inactive(database)

        view.bank_changed()
        assert view.session is None

    def test_it_is_harmless_with_no_session(self, qapp, database, chunk_id):
        repo.insert_questions(database, chunk_id, [mcq()])
        view = QuizView(database)
        view.bank_changed()
        assert view.session is None

    def test_attempts_recorded_before_it_happened_are_kept(
            self, qapp, database, chunk_id, tmp_path):
        # Nothing is salvaged, but nothing already written is thrown away.
        view = self.running(database, chunk_id, qapp)
        view.session.answer(view.session.current.correct_index)
        view.session.advance()
        assert repo.overall_accuracy(database)[1] == 1

        other_id, other_chunk = second_book(database, tmp_path)
        repo.insert_questions(database, other_chunk, [mcq(question="Cizí")])
        repo.delete_document(database, other_id)
        view.bank_changed()

        assert view.session is not None, "the wrong document was removed"
        assert repo.overall_accuracy(database)[1] == 1
