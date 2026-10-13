"""How it is going: overall, by chapter, and session by session.

The chapter table is the one worth having. Overall accuracy is a number that
barely moves after the first week; a chapter sitting at 40% while the rest are at
85% tells you what to read again tonight.

Nothing here is stored. Every figure is derived from `attempts` when the tab is
opened, which is why there is no statistics table to drift out of step with the
answers it claims to summarise.
"""

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..db import repo
from . import style
from .models import Column, RowsModel
from .tables import HoverTableView


def percent(correct, total) -> str:
    return f"{correct / total:.0%}" if total else "—"


def local_date(timestamp: str | None) -> str:
    """A stored UTC timestamp, shown as a plain local date.

    Stored in UTC and localised only here, which is the whole reason for keeping
    the storage format boring.
    """
    if not timestamp:
        return "—"
    from datetime import datetime

    try:
        return datetime.fromisoformat(timestamp).astimezone().strftime("%d %b %H:%M")
    except ValueError:
        return timestamp[:16]


class Counter(QFrame):
    """One big number with a caption under it."""

    def __init__(self, caption: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.value = QLabel("—")
        self.value.setStyleSheet(
            f"font-family: '{style.serif_family()}'; font-size: 26px; color: {style.INK};")
        layout.addWidget(self.value)

        caption_label = QLabel(caption)
        caption_label.setObjectName("meta")
        layout.addWidget(caption_label)

    def set_value(self, text) -> None:
        self.value.setText(str(text))


class StatsView(QWidget):
    """The Progress tab."""

    changed = Signal()                  # progress was reset; other tabs care

    def __init__(self, connection, parent=None):
        super().__init__(parent)
        self.setObjectName("canvas")
        self.connection = connection

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(14)

        # Which book these figures are about. Two books used to share one
        # accuracy and one chapter table, which made both useless: a chapter
        # sitting at 40% is the whole point of this tab, and it cannot be seen
        # through the average of a book that is going fine.
        source = QHBoxLayout()
        source.setSpacing(10)
        self.documents = QComboBox()
        self.documents.setMinimumWidth(320)
        source.addWidget(self.documents, 1)
        layout.addLayout(source)

        layout.addWidget(self._build_counters())
        layout.addWidget(self._build_chapters(), 3)
        layout.addWidget(self._build_sessions(), 2)

    def _build_counters(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        grid = QGridLayout(card)
        grid.setContentsMargins(24, 20, 24, 20)
        grid.setHorizontalSpacing(46)

        self.counters = {
            "accuracy": Counter(self.tr("overall accuracy")),
            "practised": Counter(self.tr("questions practised")),
            "bank": Counter(self.tr("active questions")),
            "mistakes": Counter(self.tr("in the mistakes queue")),
            "attempts": Counter(self.tr("answers recorded")),
        }
        for column, counter in enumerate(self.counters.values()):
            grid.addWidget(counter, 0, column)
        grid.setColumnStretch(len(self.counters), 1)

        # Off at the far right, away from the numbers it erases.
        self.reset_button = QPushButton(self.tr("Reset progress…"))
        self.reset_button.setObjectName("ghost")
        self.reset_button.clicked.connect(self.reset_progress)
        self.documents.currentIndexChanged.connect(self.refresh)

        # Filled here, not only on the first refresh: the picker has to
        # hold its documents before anything can choose one from it.
        self.reload_documents()
        grid.addWidget(self.reset_button, 0, len(self.counters) + 1,
                       Qt.AlignRight | Qt.AlignVCenter)
        return card

    def _build_chapters(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        heading = QHBoxLayout()
        title = QLabel(self.tr("By chapter"))
        title.setObjectName("meta")
        heading.addWidget(title)
        heading.addStretch()
        self.weakest = QLabel("")
        self.weakest.setObjectName("meta")
        heading.addWidget(self.weakest)
        layout.addLayout(heading)

        self.chapters = RowsModel([
            Column("Chapter", "chapter"),
            Column("Page", lambda row: row["page_start"] + 1, align_right=True),
            Column("Questions", "questions", align_right=True),
            Column("Answered", "attempts", align_right=True),
            Column("Accuracy",
                   lambda row: percent(row["correct"], row["attempts"]),
                   align_right=True),
        ], self)
        self.chapter_table = self._table(self.chapters, stretch_column=0)
        layout.addWidget(self.chapter_table)
        return card

    def _build_sessions(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel(self.tr("Recent sessions"))
        title.setObjectName("meta")
        layout.addWidget(title)

        self.sessions = RowsModel([
            Column("Started", lambda row: local_date(row["started_at"])),
            Column("Source", "source"),
            Column("Answered", "answered", align_right=True),
            Column("Correct", "correct", align_right=True),
            Column("Accuracy",
                   lambda row: percent(row["correct"], row["answered"]),
                   align_right=True),
            Column("Finished", lambda row: "—" if row["ended_at"] is None else "yes"),
        ], self)
        layout.addWidget(self._table(self.sessions, stretch_column=0))
        return card

    @staticmethod
    def _table(model, stretch_column: int) -> HoverTableView:
        table = HoverTableView()
        table.setModel(model)
        table.stretch_column(stretch_column)
        return table

    # --- starting over ------------------------------------------------------

    @Slot()
    def reset_progress(self) -> None:
        """Clears the record of practising, keeping the bank intact.

        The one action in the app that really deletes, so it says exactly what
        goes and what stays -- and is refused outright when there is nothing to
        clear, rather than asking a question with only one sensible answer.
        """
        totals = repo.progress_totals(self.connection)
        if not totals["attempts"] and not totals["sessions"]:
            QMessageBox.information(
                self, self.tr("Reset progress"),
                self.tr("There is no progress to reset -- no questions have been "
                        "answered yet."))
            return

        answer = QMessageBox.question(
            self, self.tr("Reset progress"),
            self.tr("Delete every answer you have recorded?\n\n"
                    "{0} answers across {1} questions, and {2} sessions, are "
                    "erased. Overall accuracy, the per-chapter figures and the "
                    "mistakes queue all go back to empty.\n\n"
                    "The questions themselves are untouched, and starred "
                    "questions stay starred. This cannot be undone.")
            .format(totals["attempts"], totals["questions"], totals["sessions"]),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return

        erased = repo.reset_progress(self.connection)
        self.refresh()
        self.changed.emit()
        QMessageBox.information(
            self, self.tr("Reset progress"),
            self.tr("Cleared {0} answers and {1} sessions. The bank still holds "
                    "{2} questions.").format(
                erased["attempts"], erased["sessions"],
                repo.count_questions(self.connection)))

    # --- loading ------------------------------------------------------------

    @Slot()
    @property
    def document_id(self) -> int | None:
        """The chosen document, or None for all of them."""
        return self.documents.currentData()

    def reload_documents(self, select_id: int | None = None) -> None:
        """Fills the picker, keeping the current choice where it can.

        Dropped entirely for a single-document bank, so nothing about this tab
        changes for someone practising one book.
        """
        wanted = select_id if select_id is not None else self.document_id
        documents = repo.list_documents(self.connection)
        several = len(documents) > 1
        self.documents.setVisible(several)

        self.documents.blockSignals(True)
        self.documents.clear()
        self.documents.addItem(self.tr("All documents"), None)
        if several:
            for row in documents:
                self.documents.addItem(row["filename"], row["id"])
            if wanted is not None:
                index = self.documents.findData(wanted)
                if index >= 0:
                    self.documents.setCurrentIndex(index)
        self.documents.blockSignals(False)

    def refresh(self) -> None:
        self.reload_documents()
        document_id = self.document_id
        correct, attempts = repo.overall_accuracy(self.connection, document_id)
        totals = repo.bank_totals(self.connection, document_id)

        self.counters["accuracy"].set_value(percent(correct, attempts))
        self.counters["practised"].set_value(
            repo.practised_count(self.connection, document_id))
        self.counters["bank"].set_value(totals["active"])
        self.counters["mistakes"].set_value(
            repo.mistakes_count(self.connection, document_id))
        self.counters["attempts"].set_value(attempts)

        chapters = repo.accuracy_by_chapter(self.connection, document_id)
        self.chapters.set_rows(chapters)
        self.weakest.setText(self._weakest_line(chapters))

        self.sessions.set_rows(
            repo.session_history(self.connection, document_id=document_id))

    def _weakest_line(self, chapters) -> str:
        """Names the chapter most worth going back to.

        Chapters with only a couple of answers are ignored: two wrong out of two
        is not a weak chapter, it is a small sample.
        """
        answered = [row for row in chapters if row["attempts"] >= 5]
        if not answered:
            return ""
        weakest = min(answered, key=lambda row: row["correct"] / row["attempts"])
        return self.tr("weakest: {0} at {1}").format(
            weakest["chapter"], percent(weakest["correct"], weakest["attempts"]))
