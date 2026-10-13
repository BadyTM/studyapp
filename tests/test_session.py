"""The rules of a practice session.

Two of them are the reason this is a class and not a few variables on a widget:
Reveal is the only way forward, and an attempt is written exactly once. Both are
easy to break with a well-meaning change to the view, and neither would announce
itself -- a session that lets you skip still looks like it works, and a double
attempt only shows up later as a mistakes queue that will not empty.
"""

import json

import pytest

from studyapp.db import connection as db_connection
from studyapp.db import repo
from studyapp.quiz.session import Outcome, Question, QuizSession, Spec


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "quiz.db"
    db_connection.ensure_schema(path)
    connection = db_connection.connect(path)
    document_id = repo.insert_document(connection, "book.pdf", "book.pdf",
                                       "sha-book", 200, "cs")
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
        "explanation": "Trvalost se týká přežití změn po pádu.",
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
def session_of(database, chunk_id):
    def make(questions, spec=None):
        repo.insert_questions(database, chunk_id, questions)
        return QuizSession.create(database, spec or Spec(limit=len(questions)))
    return make


class TestQuestionRow:
    def test_a_row_becomes_a_question(self, database, chunk_id):
        repo.insert_questions(database, chunk_id, [mcq()])
        row = repo.pick_questions(database, limit=1)[0]
        question = Question.from_row(row)

        assert question.is_mcq
        assert question.options[question.correct_index] == "Trvalost"
        assert question.chapter == "6.2 Vlastnosti transakcí"
        assert question.page == 43
        assert question.rects == [[72.0, 100.0, 400.0, 114.0]]

    def test_an_open_question_carries_its_model_answer(self, database, chunk_id):
        repo.insert_questions(database, chunk_id, [open_question()])
        question = Question.from_row(repo.pick_questions(database, limit=1)[0])
        assert not question.is_mcq
        assert question.correct_answer.startswith("Zápisem")


class TestAdvancing:
    def test_an_unanswered_question_cannot_be_stepped_past(self, session_of):
        # The rule the whole design rests on: there is no skip.
        session = session_of([mcq(), mcq(question="Druhá otázka?")])
        assert not session.can_advance
        assert session.advance() is False
        assert session.index == 0

    def test_answering_unlocks_next(self, session_of):
        session = session_of([mcq(), mcq(question="Druhá?")])
        session.answer(0)
        assert session.can_advance
        assert session.advance() is True
        assert session.index == 1

    def test_an_open_question_needs_grading_not_just_revealing(self, session_of):
        session = session_of([open_question(), open_question(question="Druhá?")])
        session.reveal()
        assert not session.can_advance, "revealing is not answering"
        session.grade(True)
        assert session.can_advance

    def test_the_session_finishes_after_the_last_question(self, session_of):
        session = session_of([mcq()])
        session.answer(3)
        session.advance()
        assert session.is_finished
        assert session.current is None

    def test_finishing_closes_the_session_row(self, database, session_of):
        session = session_of([mcq()])
        session.answer(3)
        session.advance()
        row = database.execute("SELECT ended_at FROM sessions WHERE id = ?",
                               (session.session_id,)).fetchone()
        assert row["ended_at"] is not None


class TestAnswering:
    def test_a_right_answer_is_recorded_as_correct(self, database, session_of):
        session = session_of([mcq()])
        assert session.answer(3) is True
        assert session.outcomes[0] is Outcome.CORRECT

        attempt = database.execute("SELECT * FROM attempts").fetchone()
        assert attempt["is_correct"] == 1
        assert attempt["self_graded"] == 0
        assert attempt["user_answer"] == "3"

    def test_a_wrong_answer_is_recorded_as_wrong(self, database, session_of):
        session = session_of([mcq()])
        assert session.answer(0) is False
        assert session.outcomes[0] is Outcome.WRONG
        assert database.execute("SELECT is_correct FROM attempts").fetchone()[0] == 0

    def test_answering_twice_writes_one_attempt(self, database, session_of):
        # A double click, or Enter pressed twice, must not count twice.
        session = session_of([mcq()])
        session.answer(3)
        session.answer(0)
        assert repo.count_questions(database) == 1
        assert len(database.execute("SELECT * FROM attempts").fetchall()) == 1
        assert session.outcomes[0] is Outcome.CORRECT

    def test_the_chosen_option_is_stored_as_an_index(self, database, session_of):
        # Options are shuffled at generation time; the text would not survive a
        # regeneration, the position within this question will.
        session = session_of([mcq()])
        session.answer(1)
        assert database.execute("SELECT user_answer FROM attempts").fetchone()[0] == "1"

    def test_how_long_it_took_is_recorded(self, database, session_of):
        session = session_of([mcq()])
        session.answer(3)
        assert database.execute("SELECT revealed_ms FROM attempts").fetchone()[0] >= 0

    def test_answering_an_open_question_is_a_mistake(self, session_of):
        session = session_of([open_question()])
        with pytest.raises(ValueError):
            session.answer(0)


class TestSelfGrading:
    def test_revealing_records_nothing(self, database, session_of):
        # The reader has not said whether they knew it. Guessing for them would
        # make the mistakes queue fiction.
        session = session_of([open_question()])
        session.reveal()
        assert database.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0

    def test_grading_records_a_self_graded_attempt(self, database, session_of):
        session = session_of([open_question()])
        session.reveal()
        session.grade(False)
        attempt = database.execute("SELECT * FROM attempts").fetchone()
        assert attempt["self_graded"] == 1
        assert attempt["is_correct"] == 0
        assert attempt["user_answer"] is None

    def test_grading_before_revealing_is_refused(self, session_of):
        session = session_of([open_question()])
        with pytest.raises(ValueError):
            session.grade(True)

    def test_grading_twice_writes_one_attempt(self, database, session_of):
        session = session_of([open_question()])
        session.reveal()
        session.grade(True)
        session.grade(False)
        assert len(database.execute("SELECT * FROM attempts").fetchall()) == 1

    def test_grading_an_mcq_is_a_mistake(self, session_of):
        session = session_of([mcq()])
        with pytest.raises(ValueError):
            session.grade(True)


class TestMistakesQueue:
    def test_a_wrong_answer_puts_a_question_in_the_queue(self, database, session_of):
        session = session_of([mcq()])
        session.answer(0)
        assert repo.mistakes_count(database) == 1

    def test_answering_it_right_takes_it_out_again(self, database, session_of):
        # Derived from the newest attempt, so it leaves by itself -- there is no
        # second copy of the truth to keep in step.
        session = session_of([mcq()])
        session.answer(0)
        assert repo.mistakes_count(database) == 1

        again = QuizSession.create(database, Spec(source="mistakes", limit=10))
        again.answer(3)
        assert repo.mistakes_count(database) == 0

    def test_a_deactivated_question_leaves_the_queue_quietly(self, database, session_of):
        session = session_of([mcq()])
        session.answer(0)
        database.execute("UPDATE questions SET is_active = 0")
        database.commit()
        assert repo.mistakes_count(database) == 0


class TestPicking:
    def test_types_are_respected(self, database, chunk_id):
        repo.insert_questions(database, chunk_id, [mcq(), open_question()])
        assert len(repo.pick_questions(database, types=("mcq",), limit=10)) == 1
        assert len(repo.pick_questions(database, types=("open",), limit=10)) == 1
        assert len(repo.pick_questions(database, limit=10)) == 2

    def test_inactive_questions_are_never_picked(self, database, chunk_id):
        repo.insert_questions(database, chunk_id, [mcq(), mcq()])
        database.execute("UPDATE questions SET is_active = 0 WHERE id = 1")
        database.commit()
        assert len(repo.pick_questions(database, limit=10)) == 1

    def test_the_limit_is_honoured(self, database, chunk_id):
        repo.insert_questions(database, chunk_id, [mcq() for _ in range(10)])
        assert len(repo.pick_questions(database, limit=4)) == 4

    def test_starred_only(self, database, chunk_id):
        repo.insert_questions(database, chunk_id, [mcq(), mcq()])
        first = database.execute("SELECT MIN(id) AS id FROM questions").fetchone()["id"]
        repo.set_starred(database, first, True)
        picked = repo.pick_questions(database, source="starred", limit=10)
        assert [row["id"] for row in picked] == [first]

    def test_unseen_only(self, database, session_of):
        session = session_of([mcq(), mcq(question="Druhá?")])
        session.answer(3)
        assert len(repo.pick_questions(database, source="unseen", limit=10)) == 1

    def test_by_chapter(self, database, chunk_id):
        repo.insert_questions(database, chunk_id, [mcq()])
        picked = repo.pick_questions(database, source="chapter",
                                     chapters=("6.2 Vlastnosti transakcí",), limit=10)
        assert len(picked) == 1
        assert repo.pick_questions(database, source="chapter",
                                   chapters=("Neexistuje",), limit=10) == []

    def test_no_chapters_given_returns_nothing(self, database, chunk_id):
        repo.insert_questions(database, chunk_id, [mcq()])
        assert repo.pick_questions(database, source="chapter", chapters=(), limit=10) == []

    def test_chapters_with_no_questions_are_not_offered(self, database, chunk_id):
        document_id = repo.list_documents(database)[0]["id"]
        assert repo.list_chapters(database, document_id) == []
        repo.insert_questions(database, chunk_id, [mcq()])
        assert [row["title"] for row in repo.list_chapters(database, document_id)] == [
            "6.2 Vlastnosti transakcí"]


class TestEmptySession:
    def test_nothing_to_practise_is_not_an_error(self, database):
        session = QuizSession.create(database, Spec(source="mistakes"))
        assert session.is_empty
        assert session.session_id is None, "no empty session row is opened"

    def test_no_session_row_is_written_for_an_empty_session(self, database):
        QuizSession.create(database, Spec(source="mistakes"))
        assert database.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


class TestStarring:
    def test_star_toggles_and_persists(self, database, session_of):
        session = session_of([mcq()])
        assert session.toggle_star() is True
        assert database.execute("SELECT starred FROM questions").fetchone()[0] == 1
        assert session.toggle_star() is False
        assert database.execute("SELECT starred FROM questions").fetchone()[0] == 0


class TestSummary:
    def test_counts_add_up(self, session_of):
        session = session_of([mcq() for _ in range(4)])
        session.answer(3); session.advance()
        session.answer(0); session.advance()
        session.answer(3); session.advance()

        assert session.correct_count == 2
        assert session.wrong_count == 1
        assert session.accuracy == pytest.approx(2 / 3)
        assert session.summary()["total"] == 4

    def test_an_untouched_session_has_no_accuracy_rather_than_a_crash(self, session_of):
        session = session_of([mcq()])
        assert session.accuracy == 0.0


class TestSpec:
    def test_the_header_line_reads_like_the_design(self):
        assert Spec(source="mistakes").describe() == ("mistakes", "both")
        assert Spec(source="all", types=("mcq",)).describe() == ("all", "mcq")
        assert Spec(source="chapter", types=("open",)).describe() == ("chapter", "open")

    def test_the_filter_is_recorded_on_the_session_row(self, database, session_of):
        session = session_of([mcq()], Spec(source="all", limit=1))
        row = database.execute("SELECT * FROM sessions WHERE id = ?",
                               (session.session_id,)).fetchone()
        assert json.loads(row["types_json"]) == ["mcq", "open"]
        assert json.loads(row["filter_json"])["limit"] == 1
