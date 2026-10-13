"""The practice view: one question on the left, the book on the right.

The layout is the whole point of the application, so it is worth saying what it
is for. The source is on screen the entire time, blurred, which keeps the reader
aware that the answer is *there*, in a real page of a real book, without letting
them read it. Answering brings it into focus on the right page with the sentence
highlighted. Over a few hundred questions that turns into a sense of where things
are in the book, which no flashcard deck gives you.

Widgets and signals only. What counts as answered, what may be advanced past and
what gets written down all live in quiz/session.py.
"""

from PySide6.QtCore import QSettings, Qt, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..db import repo
from ..quiz.session import Outcome, QuizSession, Spec
from . import style
from .pdf_view import SourcePane
from .session_header import SessionHeader


def restyle(widget: QWidget) -> None:
    """Makes Qt notice a changed dynamic property.

    Qt does not re-evaluate a stylesheet when a property changes -- the widget
    keeps its old look and nothing says why. Every `setProperty` that a selector
    depends on has to be followed by this.
    """
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class OptionButton(QFrame):
    """One answer option. Its result lives in a dynamic `state` property.

    A frame with a wrapping label rather than a QPushButton, because a button
    will not wrap its text: its size hint is the whole option on one line, and
    since a splitter honours the minimum width of what it holds, one long option
    was enough to shove the source pane down to a strip. Distractors are often a
    full clause, so that was most questions.
    """

    clicked = Signal()

    def __init__(self, index: int, parent=None):
        super().__init__(parent)
        self.setObjectName("option")
        self.index = index
        # Without this the focus lands on an option and Space presses it instead
        # of revealing the source.
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(15, 12, 15, 12)
        layout.setSpacing(14)

        self.number = QLabel(str(index + 1))
        self.number.setObjectName("optionNumber")
        self.number.setFixedWidth(12)
        self.number.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self.number)

        self.label = QLabel("")
        self.label.setObjectName("optionText")
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
        layout.addWidget(self.label, 1)

    def set_option(self, text: str) -> None:
        self.label.setText(text)
        self.set_state(None)

    def set_state(self, state: str | None) -> None:
        self.setProperty("state", state or "")
        restyle(self)
        # The labels carry their own colour rules, and a child is not restyled
        # by its parent changing.
        for child in (self.number, self.label):
            child.setProperty("state", state or "")
            restyle(child)

    def mousePressEvent(self, event) -> None:
        if self.isEnabled() and event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class StartScreen(QWidget):
    """What to practise, before a session begins."""

    start = Signal(object)              # Spec

    def __init__(self, connection, parent=None):
        super().__init__(parent)
        self.connection = connection

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch()

        card = QFrame()
        card.setObjectName("card")
        card.setMaximumWidth(520)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(34, 30, 34, 26)
        layout.setSpacing(18)

        heading = QLabel(self.tr("New session"))
        heading.setObjectName("question")
        layout.addWidget(heading)

        self.bank = QLabel("")
        self.bank.setObjectName("meta")
        layout.addWidget(self.bank)

        # First which book, then what from it -- the same order as the Question
        # bank tab. Hidden entirely while there is only one document, so a
        # one-book bank looks exactly as it did before.
        self.document_label = self._label(self.tr("Document"))
        layout.addWidget(self.document_label)
        self.document = QComboBox()
        layout.addWidget(self.document)
        # Unlike the chapter list this one appears once, when a second book is
        # imported, and then stays -- so it gives its space back when hidden
        # rather than leaving a blank line above "Question type" forever.
        for widget in (self.document, self.document_label):
            policy = widget.sizePolicy()
            policy.setRetainSizeWhenHidden(False)
            widget.setSizePolicy(policy)

        layout.addWidget(self._label(self.tr("Question type")))
        types = QHBoxLayout()
        self.type_group = QButtonGroup(self)
        for position, (text, value) in enumerate([
            (self.tr("Both"), ("mcq", "open")),
            (self.tr("Multiple choice"), ("mcq",)),
            (self.tr("Open"), ("open",)),
        ]):
            button = QRadioButton(text)
            button.value = value
            self.type_group.addButton(button, position)
            types.addWidget(button)
            if position == 0:
                button.setChecked(True)
        types.addStretch()
        layout.addLayout(types)

        layout.addWidget(self._label(self.tr("Draw from")))
        self.source = QComboBox()
        layout.addWidget(self.source)

        self.chapters = QComboBox()
        # Keeps its slot in the layout while hidden, so choosing "One chapter"
        # fills the gap instead of growing the card. Without this the whole card
        # jumped a line taller the moment the chapter list appeared, and a line
        # shorter again on the way back -- under the cursor, mid-decision.
        policy = self.chapters.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.chapters.setSizePolicy(policy)
        self.chapters.setVisible(False)
        layout.addWidget(self.chapters)

        count = QHBoxLayout()
        count.addWidget(self._label(self.tr("Questions")))
        self.count = QSpinBox()
        self.count.setRange(1, 200)
        self.count.setValue(20)
        count.addWidget(self.count)
        count.addStretch()
        layout.addLayout(count)

        self.begin = QPushButton(self.tr("Start"))
        self.begin.setObjectName("primary")
        layout.addWidget(self.begin, 0, Qt.AlignLeft)

        outer.addWidget(card, 0, Qt.AlignHCenter)
        outer.addStretch()

        self.begin.clicked.connect(self._emit_spec)
        self.source.currentIndexChanged.connect(self._on_source_changed)
        self.document.currentIndexChanged.connect(self.refresh)

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("meta")
        return label

    def refresh(self) -> None:
        """Reads the bank and offers only the sources that have anything in them."""
        documents = repo.list_documents(self.connection)
        self._reload_documents(documents)
        document_id = self.document.currentData()

        # Counted within the chosen document, so the figure matches what a
        # session started right now would actually draw from.
        total = repo.count_questions(self.connection, document_id=document_id)
        mistakes = repo.mistakes_count(self.connection, document_id=document_id)
        self.bank.setText(
            self.tr("{0} questions in the bank · {1} in the mistakes queue")
            .format(total, mistakes))

        self.source.blockSignals(True)
        self.source.clear()
        self.source.addItem(self.tr("All questions"), "all")
        # Offered only when there is something behind it -- an empty option that
        # starts an empty session is worse than no option.
        if mistakes:
            self.source.addItem(self.tr("Mistakes only ({0})").format(mistakes), "mistakes")
        self.source.addItem(self.tr("Starred"), "starred")
        self.source.addItem(self.tr("Not seen yet"), "unseen")
        if repo.list_chapters(self.connection, document_id):
            self.source.addItem(self.tr("One chapter"), "chapter")
        self.source.blockSignals(False)

        self.chapters.clear()
        for row in repo.list_chapters(self.connection, document_id):
            self.chapters.addItem(
                f"{row['title']}  ({row['question_count']})", row["title"])
        self.begin.setEnabled(total > 0)

    def _reload_documents(self, documents) -> None:
        """Fills the document picker, keeping the current choice where it can.

        The whole row disappears for a single-document bank: a choice between
        one book and that same book is noise, and `None` then means "all", which
        is the same set either way.
        """
        wanted = self.document.currentData()
        several = len(documents) > 1
        self.document.setVisible(several)
        self.document_label.setVisible(several)

        self.document.blockSignals(True)
        self.document.clear()
        if several:
            self.document.addItem(self.tr("All documents"), None)
            for row in documents:
                self.document.addItem(row["filename"], row["id"])
            if wanted is not None:
                index = self.document.findData(wanted)
                if index >= 0:
                    self.document.setCurrentIndex(index)
        else:
            # One document, or none: nothing to choose, so draw from everything.
            self.document.addItem(self.tr("All documents"), None)
        self.document.blockSignals(False)

    @Slot()
    def _on_source_changed(self) -> None:
        self.chapters.setVisible(self.source.currentData() == "chapter")

    @Slot()
    def _emit_spec(self) -> None:
        checked = self.type_group.checkedButton()
        self.start.emit(Spec(
            types=checked.value if checked else ("mcq", "open"),
            source=self.source.currentData() or "all",
            limit=self.count.value(),
            chapters=((self.chapters.currentData(),)
                      if self.source.currentData() == "chapter"
                      and self.chapters.currentData() else ()),
            document_id=self.document.currentData(),
        ))


class SummaryScreen(QWidget):
    """How the session went, in the same palette as everything else."""

    practise_mistakes = Signal()
    done = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch()

        card = QFrame()
        card.setObjectName("card")
        card.setMaximumWidth(520)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(34, 44, 34, 34)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignCenter)

        self.score = QLabel("—")
        self.score.setAlignment(Qt.AlignCenter)
        self.score.setStyleSheet(
            f"font-family: '{style.serif_family()}'; font-size: 46px; color: {style.INK};")
        layout.addWidget(self.score)

        self.detail = QLabel("")
        self.detail.setObjectName("meta")
        self.detail.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.detail)
        layout.addSpacing(26)

        buttons = QHBoxLayout()
        buttons.setAlignment(Qt.AlignCenter)
        self.mistakes_button = QPushButton(self.tr("Practise mistakes"))
        self.mistakes_button.setObjectName("primary")
        self.done_button = QPushButton(self.tr("Done"))
        self.done_button.setObjectName("ghost")
        buttons.addWidget(self.mistakes_button)
        buttons.addWidget(self.done_button)
        layout.addLayout(buttons)

        outer.addWidget(card, 0, Qt.AlignHCenter)
        outer.addStretch()

        self.mistakes_button.clicked.connect(self.practise_mistakes)
        self.done_button.clicked.connect(self.done)

    def show_result(self, summary: dict, mistakes_waiting: int) -> None:
        self.score.setText(f"{summary['correct']} / {summary['total']}")
        self.detail.setText(
            self.tr("{0:.0%} · {1} min · {2} in the mistakes queue")
            .format(summary["accuracy"], summary["minutes"], mistakes_waiting))
        self.mistakes_button.setVisible(mistakes_waiting > 0)


class QuestionCard(QFrame):
    """The left card: the question, the options, the controls."""

    submitted = Signal(int)             # chosen option index
    revealed = Signal()
    graded = Signal(bool)
    advanced = Signal()
    starred = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self._selected: int | None = None
        # How many options the current question has. Tracked rather than read
        # back off the buttons: `isVisible()` answers "is this painted right
        # now", which is false for every child of a window not yet shown, and is
        # the wrong question to ask about what the question actually holds.
        self._option_count = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 34, 34, 28)
        layout.setSpacing(0)

        self.question = QLabel("")
        self.question.setObjectName("question")
        self.question.setWordWrap(True)
        self.question.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        # Around 33 characters a line, set by capping the width rather than by
        # inserting breaks -- hand-wrapped text comes apart on the first resize.
        self.question.setMaximumWidth(style.question_width())
        layout.addWidget(self.question)
        layout.addSpacing(30)

        self.options = [OptionButton(index) for index in range(4)]
        for button in self.options:
            button.clicked.connect(
                lambda index=button.index: self.select(index))
            layout.addWidget(button)
            layout.addSpacing(9)

        self.answer = QLabel("")
        self.answer.setObjectName("explanation")
        self.answer.setWordWrap(True)
        self.answer.setVisible(False)
        layout.addWidget(self.answer)

        layout.addStretch()

        controls = QHBoxLayout()
        controls.setSpacing(12)
        self.primary = QPushButton(self.tr("Submit"))
        self.primary.setObjectName("primary")
        self.star = QPushButton(self.tr("☆ Star"))
        self.star.setObjectName("ghost")
        self.yes = QPushButton(self.tr("I knew it"))
        self.yes.setObjectName("gradeYes")
        self.no = QPushButton(self.tr("I didn't"))
        self.no.setObjectName("gradeNo")
        self.hint = QLabel("")
        self.hint.setObjectName("hint")
        # A quiet reminder, not a control: it gives way before the buttons do
        # when the pane is dragged narrow.
        self.hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        for widget in (self.primary, self.yes, self.no, self.star):
            controls.addWidget(widget)
        controls.addWidget(self.hint)
        controls.addStretch()
        layout.addLayout(controls)

        self.primary.clicked.connect(self._on_primary)
        self.yes.clicked.connect(lambda: self.graded.emit(True))
        self.no.clicked.connect(lambda: self.graded.emit(False))
        self.star.clicked.connect(self.starred)

    # --- filling in ---------------------------------------------------------

    def show_question(self, question, revealed: bool = False) -> None:
        self._selected = None
        self.question.setText(question.text)
        self.answer.setVisible(False)
        self.star.setText(self.tr("★ Starred") if question.starred
                          else self.tr("☆ Star"))

        self._option_count = len(question.options) if question.is_mcq else 0
        for index, button in enumerate(self.options):
            shown = index < self._option_count
            button.setVisible(shown)
            button.setEnabled(True)
            if shown:
                button.set_option(question.options[index])

        if question.is_mcq:
            self.primary.setText(self.tr("Submit"))
            self.primary.setEnabled(False)
            self.hint.setText(self.tr("1–4 select · ⏎ submit"))
        else:
            self.primary.setText(self.tr("Reveal answer"))
            self.primary.setEnabled(True)
            self.hint.setText(self.tr("␣ reveal · answer in your head first"))

        self.primary.setVisible(True)
        self.yes.setVisible(False)
        self.no.setVisible(False)

    def show_result(self, question, outcome: Outcome, chosen: int | None) -> None:
        """Marks the options and shows the explanation, after an answer."""
        for index, button in enumerate(self.options[:self._option_count]):
            button.setEnabled(False)
            if index == question.correct_index:
                button.set_state("correct")
            elif index == chosen:
                button.set_state("wrong")
            else:
                button.set_state("dimmed")

        self._show_explanation(question)
        self.primary.setText(self.tr("Next"))
        self.primary.setEnabled(True)
        self.hint.setText(self.tr("→ next"))

    def show_open_answer(self, question) -> None:
        """The model answer for an open question, before self-grading."""
        self._show_explanation(question, include_answer=True)
        self.primary.setVisible(False)
        self.yes.setVisible(True)
        self.no.setVisible(True)
        self.hint.setText(self.tr("Y knew it · N didn't"))

    def show_next_only(self) -> None:
        """After grading an open question: nothing left but to move on."""
        self.yes.setVisible(False)
        self.no.setVisible(False)
        self.primary.setVisible(True)
        self.primary.setText(self.tr("Next"))
        self.primary.setEnabled(True)
        self.hint.setText(self.tr("→ next"))

    def _show_explanation(self, question, include_answer: bool = False) -> None:
        parts = []
        if include_answer and question.correct_answer:
            parts.append(question.correct_answer)
        if question.explanation:
            parts.append(question.explanation)
        self.answer.setText("\n\n".join(parts))
        self.answer.setVisible(bool(parts))

    # --- input --------------------------------------------------------------

    def select(self, index: int) -> None:
        """Picks an option, by click or by number key. Ignored after the answer."""
        if index >= self._option_count or not self.options[index].isEnabled():
            return
        self._selected = index
        for position, button in enumerate(self.options[:self._option_count]):
            button.set_state("selected" if position == index else None)
        self.primary.setEnabled(True)

    @Slot()
    def _on_primary(self) -> None:
        text = self.primary.text()
        if text == self.tr("Next"):
            self.advanced.emit()
        elif text == self.tr("Reveal answer"):
            self.revealed.emit()
        elif self._selected is not None:
            self.submitted.emit(self._selected)


class QuizView(QWidget):
    """Splitter, screens, keyboard. Everything else is the session's business."""

    def __init__(self, connection, parent=None):
        super().__init__(parent)
        self.setObjectName("canvas")
        self.connection = connection
        self.session: QuizSession | None = None
        self._settings = QSettings(config.ORG_NAME, config.APP_NAME)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(14)

        self.header = SessionHeader()
        layout.addWidget(self.header)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.left = QStackedWidget()
        self.start_screen = StartScreen(connection)
        self.card = QuestionCard()
        self.summary = SummaryScreen()
        self.left.addWidget(self.start_screen)      # 0
        self.left.addWidget(self.card)              # 1
        self.left.addWidget(self.summary)           # 2

        self.source = SourcePane()
        self.left.setMinimumWidth(360)
        self.source.setMinimumWidth(360)
        self.splitter.addWidget(self.left)
        self.splitter.addWidget(self.source)
        # Equal halves, and equal shares of any resize.
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([500, 500])
        layout.addWidget(self.splitter, 1)

        self._connect()
        self._install_shortcuts()
        self._restore_splitter()
        self.show_start()

    def _connect(self) -> None:
        self.start_screen.start.connect(self.begin_session)
        self.card.submitted.connect(self._on_submit)
        self.card.revealed.connect(self._on_reveal)
        self.card.graded.connect(self._on_grade)
        self.card.advanced.connect(self._on_advance)
        self.card.starred.connect(self._on_star)
        self.summary.practise_mistakes.connect(self._practise_mistakes)
        self.summary.done.connect(self.show_start)

    def _install_shortcuts(self) -> None:
        """Keys, as shortcuts rather than key handlers.

        A `keyPressEvent` on the card would never see Space once a button had
        focus -- the button would swallow it. Shortcuts scoped to this widget
        also keep the Generate tab from reacting to a stray "1".
        """
        def add(sequence, slot):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
            return shortcut

        for number in range(4):
            add(str(number + 1), lambda index=number: self.card.select(index))
        add(Qt.Key_Return, self.card._on_primary)
        add(Qt.Key_Enter, self.card._on_primary)
        add(Qt.Key_Space, self._on_space)
        add(Qt.Key_Right, self._on_advance)
        add("Y", lambda: self._on_grade(True))
        add("N", lambda: self._on_grade(False))
        add("S", self._on_star)

    def _describe_spec(self, spec: Spec) -> str:
        """The line beside the dots: what this session was drawn from."""
        sources = {
            "all": self.tr("whole bank"),
            "mistakes": self.tr("mistakes only"),
            "starred": self.tr("starred only"),
            "unseen": self.tr("unseen only"),
            "chapter": self.tr("selected chapters"),
        }
        kinds = {
            "mcq": self.tr("multiple choice"),
            "open": self.tr("open"),
            "both": self.tr("both types"),
        }
        source, kind = spec.describe()
        return f"{sources.get(source, source)} · {kinds.get(kind, kind)}"

    # --- screens ------------------------------------------------------------

    def show_start(self) -> None:
        self.session = None
        self.start_screen.refresh()
        self.header.set_session([], 0, self.tr("ready"))
        self.left.setCurrentIndex(0)
        self.source.lock()

    @Slot(object)
    def begin_session(self, spec: Spec) -> None:
        session = QuizSession.create(self.connection, spec)
        if session.is_empty:
            self.start_screen.bank.setText(
                self.tr("Nothing matches that. Try a different source."))
            return
        self.session = session
        first = session.current
        self.source.set_document(first.pdf_path, first.document_sha)
        self.left.setCurrentIndex(1)
        self._show_current()

    def _show_current(self) -> None:
        session = self.session
        if session is None or session.is_finished:
            self._finish()
            return
        self.card.show_question(session.current)
        # Locked first, always. The pane is blurred and its metadata blank before
        # the question is even readable.
        self.source.lock()
        self._update_header()

    def _update_header(self) -> None:
        if self.session is None:
            return
        self.header.set_session(self.session.outcomes, self.session.index,
                                self._describe_spec(self.session.spec))

    def _finish(self) -> None:
        if self.session is None:
            return
        self.session.finish()
        self.summary.show_result(self.session.summary(),
                                 repo.mistakes_count(self.connection))
        self.left.setCurrentIndex(2)
        self.source.lock()

    # --- answering ----------------------------------------------------------

    @Slot(int)
    def _on_submit(self, chosen: int) -> None:
        session = self.session
        if session is None or session.current is None:
            return
        question = session.current
        session.answer(chosen)
        self.card.show_result(question, session.current_outcome, chosen)
        self._reveal_source(question)
        self._update_header()

    @Slot()
    def _on_reveal(self) -> None:
        session = self.session
        if session is None or session.current is None or session.revealed:
            return
        question = session.current
        session.reveal()
        self.card.show_open_answer(question)
        self._reveal_source(question)

    @Slot()
    def _on_space(self) -> None:
        """Space reveals an open question, and does nothing else.

        On a multiple-choice question there is nothing to reveal until an answer
        is in, and making Space submit as well would turn a stray keypress into
        a recorded wrong answer.
        """
        session = self.session
        if session and session.current and not session.current.is_mcq:
            self._on_reveal()

    @Slot(bool)
    def _on_grade(self, knew_it: bool) -> None:
        session = self.session
        if session is None or session.current is None:
            return
        if session.current.is_mcq or not session.revealed:
            return
        if session.current_outcome is not Outcome.UNANSWERED:
            return
        session.grade(knew_it)
        self.card.show_next_only()
        self._update_header()

    @Slot()
    def _on_advance(self) -> None:
        session = self.session
        if session is None:
            return
        if not session.advance():
            return                      # Reveal is the only way forward
        self._show_current()

    @Slot()
    def _on_star(self) -> None:
        if self.session is None or self.session.current is None:
            return
        starred = self.session.toggle_star()
        self.card.star.setText(self.tr("★ Starred") if starred else self.tr("☆ Star"))

    def _reveal_source(self, question) -> None:
        # Re-stated every time rather than once when the session began: the
        # document can be relocated mid-session, and a session that outlived the
        # move would go on pointing at where the file used to be.
        self.source.set_document(question.pdf_path, question.document_sha)
        self.source.reveal(question.page, question.source_quote,
                           question.chapter, question.page_count, question.rects)

    @Slot()
    def document_moved(self) -> None:
        """Takes up a new location for the PDF without ending the session.

        Each question carries the path it was loaded with, so relocating the
        document leaves an open session holding the old one -- the pane says the
        file is missing while the database already knows better.
        """
        if self.session is None:
            return
        shas = {q.document_sha for q in self.session.questions if q.document_sha}
        for sha in shas:
            document = repo.get_document_by_sha(self.connection, sha)
            if document is None:
                continue
            for question in self.session.questions:
                if question.document_sha == sha:
                    question.pdf_path = document["path"]

        current = self.session.current
        if current is None:
            return
        self.source.set_document(current.pdf_path, current.document_sha)
        # If the reader is looking at the broken message right now, replace it
        # with the page rather than making them advance to see the fix.
        if self.session.revealed:
            self._reveal_source(current)

    @Slot(str)
    def preview_ready(self, document_sha: str) -> None:
        """Puts the blurred page up now that it has been rendered.

        The pane reads its page off disk and says "preview not ready" when the
        file is not there, which is what an open session shows while the render
        is still going. Briefly, for a book imported here -- but for the whole
        session on a bank that arrived from someone else, where the cache does
        not exist at all and the render only starts once the PDF is found.

        Every question re-reads the page, so it would come right by itself at
        the next card. This is so the card on screen does not have to be sat
        out: the reader asked for the source, not for a reason to press on.
        """
        if self.session is None:
            return
        current = self.session.current
        if current is None or current.document_sha != document_sha:
            return
        if self.session.revealed:
            self._reveal_source(current)
        else:
            # Idempotent: the pane is already locked, this only makes it
            # re-read the page it could not find a moment ago.
            self.source.lock()

    @Slot()
    def _practise_mistakes(self) -> None:
        self.begin_session(Spec(source="mistakes", limit=20))

    # --- geometry -----------------------------------------------------------

    def _restore_splitter(self) -> None:
        state = self._settings.value("quiz/splitter2")
        if state is not None:
            self.splitter.restoreState(state)

    def save_state(self) -> None:
        self._settings.setValue("quiz/splitter2", self.splitter.saveState())

    @Slot()
    def bank_changed(self) -> None:
        """Ends the session if the questions in it have been deleted.

        A session is a list of questions read into memory when it started, so it
        outlives anything that happens to them afterwards. Remove their document
        on the Question bank tab and the session carries on asking them -- and
        answering one writes an attempt against a row that is gone, which fails
        on the foreign key. Under pythonw that traceback goes nowhere: the
        reader sees a question that will not accept an answer.

        Nothing is salvaged here. Half a session over a bank that has just
        changed underneath it is not worth continuing, and the attempts recorded
        before it happened are already in the database.
        """
        session = self.session
        if session is None or session.is_empty:
            return

        asked = [question.id for question in session.questions]
        alive = repo.existing_question_ids(self.connection, asked)
        if len(alive) == len(asked):
            return

        session.finish()
        self.show_start()
        self.start_screen.bank.setText(self.tr(
            "That session ended: its questions are no longer in the bank."))
