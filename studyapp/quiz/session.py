"""One practice session, from the first question to the summary.

Two rules are enforced here rather than left to the window to remember:

  * **Reveal is the only way forward.** There is no skip. A question is left
    behind only once it has been answered or graded, which is what stops the
    session becoming a list to browse rather than a test to sit.
  * **An attempt is written exactly once**, the moment the answer is known, not
    at the end. Close the app half way through and everything answered so far is
    already recorded.

Nothing here imports Qt.
"""

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

from ..db import repo


class Outcome(str, Enum):
    UNANSWERED = "unanswered"
    CORRECT = "correct"
    WRONG = "wrong"


@dataclass
class Question:
    """A question as the quiz needs it, with the source it came from."""

    id: int
    type: str
    text: str
    explanation: str | None
    source_quote: str
    page: int | None            # 0-based; add one only when showing it
    rects: list[list[float]]
    chapter: str | None
    starred: bool
    options: list[str] = field(default_factory=list)
    correct_index: int | None = None
    correct_text: str | None = None
    pdf_path: str | None = None
    document_sha: str | None = None
    page_count: int = 0

    @classmethod
    def from_row(cls, row) -> "Question":
        return cls(
            id=row["id"],
            type=row["type"],
            text=row["question"],
            explanation=row["explanation"],
            source_quote=row["source_quote"],
            page=row["page"],
            rects=json.loads(row["rects_json"]) if row["rects_json"] else [],
            chapter=row["chapter"],
            starred=bool(row["starred"]),
            options=json.loads(row["options_json"]) if row["options_json"] else [],
            correct_index=row["correct_index"],
            correct_text=row["correct_text"],
            pdf_path=row["pdf_path"],
            document_sha=row["document_sha"],
            page_count=row["page_count"],
        )

    @property
    def is_mcq(self) -> bool:
        return self.type == "mcq"

    @property
    def correct_answer(self) -> str:
        if self.is_mcq and self.correct_index is not None:
            return self.options[self.correct_index]
        return self.correct_text or ""


@dataclass
class Spec:
    """What the reader asked for when starting the session."""

    types: tuple[str, ...] = ("mcq", "open")
    source: str = "all"                 # all | mistakes | chapter | starred | unseen
    limit: int = 20
    chapters: tuple[str, ...] = ()
    document_id: int | None = None

    def describe(self) -> tuple[str, str]:
        """What the session draws from, as two keys for the window to word.

        Keys rather than a sentence, because the sentence is painted into the
        session header and everything else on screen goes through `tr()`. This
        file keeps no Qt, so it cannot translate anything -- and a dataclass is
        not a QObject, so it could not call `tr()` even if it imported it.
        """
        kind = {("mcq",): "mcq", ("open",): "open"}.get(tuple(self.types), "both")
        return self.source, kind


class QuizSession:
    """The state of one run through a set of questions."""

    def __init__(self, connection, questions: Sequence[Question],
                 spec: Spec, session_id: int | None = None):
        self.connection = connection
        self.questions = list(questions)
        self.spec = spec
        self.session_id = session_id
        self.outcomes: list[Outcome] = [Outcome.UNANSWERED] * len(self.questions)
        self.index = 0
        self.revealed = False
        self._shown_at = time.monotonic()
        self._started_at = time.monotonic()

    # --- creating -----------------------------------------------------------

    @classmethod
    def create(cls, connection, spec: Spec) -> "QuizSession":
        """Picks the questions and opens a row in `sessions`.

        Returns a session with no questions when nothing matches -- an empty
        mistakes queue is a perfectly good answer, and better than an error.
        """
        rows = repo.pick_questions(
            connection,
            types=spec.types,
            source=spec.source,
            limit=spec.limit,
            document_id=spec.document_id,
            chapters=spec.chapters,
        )
        if not rows:
            return cls(connection, [], spec, session_id=None)

        session_id = repo.start_session(
            connection, spec.types, spec.source,
            {"limit": spec.limit, "chapters": list(spec.chapters),
             "document_id": spec.document_id},
        )
        return cls(connection, [Question.from_row(row) for row in rows], spec, session_id)

    # --- where we are -------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self.questions)

    @property
    def is_empty(self) -> bool:
        return not self.questions

    @property
    def is_finished(self) -> bool:
        return self.index >= self.total

    @property
    def current(self) -> Question | None:
        return None if self.is_finished else self.questions[self.index]

    @property
    def position(self) -> int:
        """1-based, for showing. `index` stays 0-based for indexing."""
        return min(self.index + 1, self.total)

    @property
    def current_outcome(self) -> Outcome:
        return (Outcome.UNANSWERED if self.is_finished
                else self.outcomes[self.index])

    @property
    def can_advance(self) -> bool:
        """Whether Next is allowed yet.

        False until the current question has been answered or graded. There is no
        skip: a question you can step past without answering is one you will step
        past every time it is hard.
        """
        return not self.is_finished and self.current_outcome is not Outcome.UNANSWERED

    # --- answering ----------------------------------------------------------

    def answer(self, chosen_index: int) -> bool:
        """Records an answer to the current multiple-choice question.

        Answering twice does nothing: the first answer stands, and a double-click
        or a repeated keypress must not write a second attempt.
        """
        question = self.current
        if question is None or not question.is_mcq:
            raise ValueError("not on a multiple-choice question")
        if self.current_outcome is not Outcome.UNANSWERED:
            return self.current_outcome is Outcome.CORRECT

        correct = chosen_index == question.correct_index
        self.outcomes[self.index] = Outcome.CORRECT if correct else Outcome.WRONG
        self.revealed = True
        repo.record_attempt(
            self.connection,
            session_id=self.session_id,
            question_id=question.id,
            is_correct=correct,
            # The index, not the text: options are shuffled at generation time
            # and the string would not survive a regeneration.
            user_answer=str(chosen_index),
            self_graded=False,
            revealed_ms=self._elapsed_ms(),
        )
        return correct

    def reveal(self) -> None:
        """Shows the answer and the source for an open question.

        Nothing is recorded here. The reader has not said yet whether they knew
        it, and guessing on their behalf would make the mistakes queue fiction.
        """
        if self.current is None:
            return
        self.revealed = True

    def grade(self, knew_it: bool) -> None:
        """Records the reader's own verdict on an open question."""
        question = self.current
        if question is None or question.is_mcq:
            raise ValueError("not on an open question")
        if not self.revealed:
            raise ValueError("grade before reveal")
        if self.current_outcome is not Outcome.UNANSWERED:
            return

        self.outcomes[self.index] = Outcome.CORRECT if knew_it else Outcome.WRONG
        repo.record_attempt(
            self.connection,
            session_id=self.session_id,
            question_id=question.id,
            is_correct=knew_it,
            user_answer=None,
            self_graded=True,
            revealed_ms=self._elapsed_ms(),
        )

    def advance(self) -> bool:
        """Moves to the next question. Returns False if not allowed yet."""
        if not self.can_advance:
            return False
        self.index += 1
        self.revealed = False
        self._shown_at = time.monotonic()
        if self.is_finished:
            self.finish()
        return True

    def toggle_star(self) -> bool:
        question = self.current
        if question is None:
            return False
        question.starred = not question.starred
        repo.set_starred(self.connection, question.id, question.starred)
        return question.starred

    def finish(self) -> None:
        if self.session_id is not None:
            repo.end_session(self.connection, self.session_id)

    def _elapsed_ms(self) -> int:
        return int((time.monotonic() - self._shown_at) * 1000)

    # --- results ------------------------------------------------------------

    @property
    def correct_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome is Outcome.CORRECT)

    @property
    def wrong_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome is Outcome.WRONG)

    @property
    def answered_count(self) -> int:
        return self.correct_count + self.wrong_count

    @property
    def accuracy(self) -> float:
        return self.correct_count / self.answered_count if self.answered_count else 0.0

    @property
    def elapsed_minutes(self) -> int:
        return int((time.monotonic() - self._started_at) / 60)

    def summary(self) -> dict:
        return {
            "correct": self.correct_count,
            "wrong": self.wrong_count,
            "total": self.total,
            "accuracy": self.accuracy,
            "minutes": self.elapsed_minutes,
        }
