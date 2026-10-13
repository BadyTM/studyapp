"""The question bank: find one, read it, fix it or retire it.

Six hundred generated questions will contain some that are wrong, some that are
badly worded and a few that ask about a figure caption. This is where they get
dealt with.

It opens on the whole active bank rather than on "needs review". That was the
first design and it was wrong the moment generation started going well: with
every quote placed and every score at 1.00, nothing needs reviewing and the tab
opened onto an empty table beside a full bank, which reads as a fault. The
review filter carries its count in its own label instead, which is what tells
you whether it is worth opening.

Three things this view deliberately will not do:

  * **Edit the quote.** `source_quote` is read-only. A quote edited by hand no
    longer matches the text it was located in, which breaks the highlight
    silently and papers over the fact that the model paraphrased. A question with
    a bad quote is a bad question: retire it, or regenerate its chunk.
  * **Edit options as raw JSON in a cell.** `options_json` and `correct_index`
    point at each other, and editing one in a grid is how they stop agreeing. A
    small dialog owns both.
  * **Delete anything with history.** Attempts hang off these rows.
"""

import json
import sqlite3
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QItemSelectionModel,
    QModelIndex,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..db import repo, transfer
from ..ingest import blur
from . import style
from .tables import HoverTableView


class QuestionTableModel(QAbstractTableModel):
    """The bank as a grid. Reads through repo, writes through repo."""

    COLUMNS = ("Star", "Type", "Chapter", "Page", "Question", "Score")
    STAR, TYPE, CHAPTER, PAGE, QUESTION, SCORE = range(6)

    def __init__(self, connection, parent=None):
        super().__init__(parent)
        self.connection = connection
        self._rows: list = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return self.tr(self.COLUMNS[section])
        if role == Qt.TextAlignmentRole:
            # Over its own column, not floating in the middle of it. Qt centres
            # headings by default, which left every heading out of line with the
            # cells beneath it -- and, once the filters moved under the headings,
            # out of line with those too.
            if section in (self.PAGE, self.SCORE):
                return int(Qt.AlignRight | Qt.AlignVCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = index.column()

        if role == Qt.DisplayRole:
            if column == self.STAR:
                return "★" if row["starred"] else ""
            if column == self.TYPE:
                return row["type"]
            if column == self.CHAPTER:
                return row["chapter"] or "—"
            if column == self.PAGE:
                # 0-based in the database, 1-based on the page itself.
                return row["page"] + 1 if row["page"] is not None else "—"
            if column == self.QUESTION:
                return row["question"]
            if column == self.SCORE:
                score = row["quote_score"]
                return f"{score:.2f}" if score is not None else "—"

        if role == Qt.ForegroundRole:
            if not row["is_active"]:
                return QColor(style.INK_TERTIARY)
            if column == self.SCORE and (row["quote_score"] or 0) < 0.95:
                return QColor(style.WRONG_TEXT)
            if column == self.PAGE and row["page"] is None:
                return QColor(style.WRONG_TEXT)

        if role == Qt.ToolTipRole:
            return self._tooltip(row)

        if role == Qt.TextAlignmentRole and column in (self.PAGE, self.SCORE):
            return int(Qt.AlignRight | Qt.AlignVCenter)

        return None

    def _tooltip(self, row) -> str:
        parts = [row["question"]]
        if row["type"] == "mcq" and row["options_json"]:
            options = json.loads(row["options_json"])
            parts += [f"{index + 1}. {option}"
                      + ("   ←" if index == row["correct_index"] else "")
                      for index, option in enumerate(options)]
        elif row["correct_text"]:
            parts.append(row["correct_text"])
        parts.append("")
        parts.append(f"“{row['source_quote']}”")
        if not row["is_active"]:
            parts.append("(retired)")
        if row["edited_at"]:
            parts.append("(edited by hand)")
        return "\n".join(parts)

    # --- loading and writing ------------------------------------------------

    def reload(self, **filters) -> None:
        self.beginResetModel()
        self._rows = repo.search_questions(self.connection, **filters)
        self.endResetModel()

    def row_at(self, position: int):
        return self._rows[position]

    def ids_at(self, positions) -> list[int]:
        return [self._rows[position]["id"] for position in positions]


class EditDialog(QDialog):
    """Editing one question.

    The options live here rather than in the grid because `correct_index` points
    into them: four fields and a radio button keep the two in step by
    construction, where a JSON string in a cell relies on nobody making a typo.
    """

    def __init__(self, row, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Edit question"))
        self.setMinimumWidth(620)
        self.row = row

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(self._label(self.tr("Question")))
        self.question = QPlainTextEdit(row["question"])
        self.question.setFixedHeight(70)
        layout.addWidget(self.question)

        self.is_mcq = row["type"] == "mcq"
        self.options: list[QLineEdit] = []
        self.correct_group = QButtonGroup(self)

        if self.is_mcq:
            layout.addWidget(self._label(self.tr("Options — select the correct one")))
            existing = json.loads(row["options_json"]) if row["options_json"] else []
            for index in range(4):
                line = QHBoxLayout()
                radio = QRadioButton()
                radio.setChecked(index == row["correct_index"])
                self.correct_group.addButton(radio, index)
                field = QLineEdit(existing[index] if index < len(existing) else "")
                self.options.append(field)
                line.addWidget(radio)
                line.addWidget(field, 1)
                layout.addLayout(line)
        else:
            layout.addWidget(self._label(self.tr("Model answer")))
            self.correct_text = QPlainTextEdit(row["correct_text"] or "")
            self.correct_text.setFixedHeight(70)
            layout.addWidget(self.correct_text)

        layout.addWidget(self._label(self.tr("Explanation")))
        self.explanation = QPlainTextEdit(row["explanation"] or "")
        self.explanation.setFixedHeight(60)
        layout.addWidget(self.explanation)

        # Shown because it is the anchor for everything else, and read-only
        # because editing it would break the link to the page it was found on.
        layout.addWidget(self._label(
            self.tr("Source quote — read-only, page {0}").format(
                row["page"] + 1 if row["page"] is not None else "?")))
        quote = QPlainTextEdit(row["source_quote"])
        quote.setReadOnly(True)
        quote.setFixedHeight(60)
        quote.setStyleSheet(f"color: {style.INK_SECONDARY};")
        layout.addWidget(quote)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("meta")
        return label

    def changes(self) -> dict:
        """The fields to save. Options and their index always travel together."""
        fields = {
            "question": self.question.toPlainText().strip(),
            "explanation": self.explanation.toPlainText().strip() or None,
        }
        if self.is_mcq:
            # The radio button's id is a position among the four fields; the list
            # that gets saved has the blank ones taken out. Carrying the id
            # across unchanged moved the right answer: clear option A and the
            # mark that pointed at B pointed at C, with nothing said about it.
            # So the surviving fields are paired with their positions and the
            # mark is looked up by position, not by count.
            kept = [(position, field.text().strip())
                    for position, field in enumerate(self.options)
                    if field.text().strip()]
            options = [text for _, text in kept]
            fields["options_json"] = json.dumps(options, ensure_ascii=False)

            chosen = self.correct_group.checkedId()
            moved_to = [new for new, (old, _) in enumerate(kept) if old == chosen]
            fields["correct_index"] = moved_to[0] if moved_to else 0
        else:
            fields["correct_text"] = self.correct_text.toPlainText().strip()
        return fields

    def problem(self) -> str | None:
        """Why this cannot be saved, if it cannot."""
        if not self.question.toPlainText().strip():
            return self.tr("The question cannot be empty.")
        if self.is_mcq:
            filled = [field.text().strip() for field in self.options
                      if field.text().strip()]
            if len(filled) < 2:
                return self.tr("A multiple-choice question needs at least two options.")
            if len(set(filled)) != len(filled):
                return self.tr("Two options are identical.")
            chosen = self.correct_group.checkedId()
            if chosen < 0:
                return self.tr("Mark which option is correct.")
            # Whether the marked field is empty, not whether its position is
            # past the end of the shortened list -- which rejected clearing B
            # while D was the answer, and let clearing A through while B was.
            if not self.options[chosen].text().strip():
                return self.tr("The correct option is empty.")
        elif not self.correct_text.toPlainText().strip():
            return self.tr("The model answer cannot be empty.")
        return None


class BankView(QWidget):
    """Search, filter, edit, retire."""

    changed = Signal()
    # A document was pointed at its PDF again. The Generate tab owns the thread
    # pool, so it is the one that can re-render the blurred pages.
    document_relocated = Signal(int)

    def __init__(self, connection, parent=None):
        super().__init__(parent)
        self.setObjectName("canvas")
        self.connection = connection

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(12)

        # Which book first, everything else after. The bank can hold questions
        # from several documents and almost nothing about one applies to another:
        # the chapter titles differ, and a page number only means anything
        # against the file it was found in.
        source = QHBoxLayout()
        source.setSpacing(10)
        self.documents = QComboBox()
        self.documents.setMinimumWidth(320)
        source.addWidget(self.documents, 1)

        # Shown only when the chosen document's file is not where it was
        # imported from -- the normal state of a bank whose folder was renamed,
        # or one opened on another machine. It lives here rather than on the
        # Generate tab because this is where a document is chosen.
        self.locate_button = QPushButton(self.tr("Locate PDF…"))
        self.locate_button.setObjectName("primary")
        self.locate_button.setVisible(False)
        source.addWidget(self.locate_button)

        # Up here rather than with Edit and Retire, because these act on the
        # whole document the picker is showing -- not on the rows that happen to
        # be selected, which is what everything along the bottom does.
        self.export_button = QPushButton(self.tr("Export…"))
        self.export_button.setObjectName("ghost")
        source.addWidget(self.export_button)
        self.import_button = QPushButton(self.tr("Import…"))
        self.import_button.setObjectName("ghost")
        source.addWidget(self.import_button)

        # The only control in the app that destroys a bank. Offered for one
        # chosen document, never for "All documents": there is no reading of
        # that click that is safe to guess at.
        self.remove_button = QPushButton(self.tr("Remove…"))
        self.remove_button.setObjectName("ghost")
        source.addWidget(self.remove_button)

        # Which slice of the bank is on screen, rather than a filter on a column:
        # it has no heading to sit under, and its label carries a count.
        #
        # Active first, and it is the default. "Needs review" used to be, which
        # was right in theory and wrong in practice: when generation goes well
        # nothing needs reviewing, so opening the tab showed an empty table
        # beside a full bank. The count in its label says whether it is worth
        # clicking, which is what the default was really for.
        self.state = QComboBox()
        for text, value in [
            (self.tr("Active"), "active"),
            (self.tr("Needs review"), "review"),
            (self.tr("Retired"), "inactive"),
            (self.tr("All"), "all"),
        ]:
            self.state.addItem(text, value)
        source.addWidget(self.state)
        layout.addLayout(source)

        # The rest live under the column they filter, where the heading says
        # what they do and no label of their own is needed.
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("Search…"))
        self.search.setClearButtonEnabled(True)

        self.type_filter = QComboBox()
        for text, value in [(self.tr("Any type"), None),
                            (self.tr("Multiple choice"), "mcq"),
                            (self.tr("Open"), "open")]:
            self.type_filter.addItem(text, value)

        self.chapter_filter = QComboBox()

        # Under the star column, so it needs no word to explain itself.
        self.starred_only = QCheckBox()
        self.starred_only.setToolTip(self.tr("Starred only"))

        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(10)

        self.count = QLabel("")
        self.count.setObjectName("meta")
        card_layout.addWidget(self.count)

        self.model = QuestionTableModel(connection, self)
        self.table = HoverTableView()
        self.table.setModel(self.model)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.stretch_column(QuestionTableModel.QUESTION)
        self.table.doubleClicked.connect(self._edit_row)

        # Each filter under the column it acts on. Page and Score have none --
        # the empty space under them says so better than a disabled control.
        #
        # The widths are the filters' and not the content's. Sized to their
        # contents these columns are three characters of `mcq` and whatever the
        # longest chapter title happens to be: the first clipped its combo box
        # down to an arrow, and the second took half the window for titles that
        # need a quarter of it, pushing Question off the edge.
        # No width: now that the column has a heading, letting Qt size it to
        # "Star" gives a number that follows whatever font is in force, which a
        # constant here could only guess at. The box is centred in whatever
        # comes out, and needs far less.
        self.table.filters.set_widget(QuestionTableModel.STAR, self.starred_only)
        self.table.filters.set_widget(
            QuestionTableModel.TYPE, self.type_filter, width=132)
        self.table.filters.set_widget(
            QuestionTableModel.CHAPTER, self.chapter_filter, width=240)
        self.table.filters.set_widget(QuestionTableModel.QUESTION, self.search)
        card_layout.addWidget(self.table, 1)
        layout.addWidget(card, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.edit_button = QPushButton(self.tr("Edit…"))
        self.edit_button.setObjectName("primary")
        self.retire_button = QPushButton(self.tr("Retire"))
        self.retire_button.setObjectName("ghost")
        self.restore_button = QPushButton(self.tr("Restore"))
        self.restore_button.setObjectName("ghost")
        self.star_button = QPushButton(self.tr("Toggle star"))
        self.star_button.setObjectName("ghost")
        self.purge_button = QPushButton(self.tr("Purge retired…"))
        self.purge_button.setObjectName("ghost")
        for button in (self.edit_button, self.retire_button, self.restore_button,
                       self.star_button):
            buttons.addWidget(button)
        buttons.addStretch()
        buttons.addWidget(self.purge_button)
        layout.addLayout(buttons)

        self.search.textChanged.connect(self.refresh)
        self.state.currentIndexChanged.connect(self.refresh)
        self.type_filter.currentIndexChanged.connect(self.refresh)
        self.chapter_filter.currentIndexChanged.connect(self.refresh)
        self.starred_only.toggled.connect(self.refresh)
        # Changing the document rebuilds the chapter list before refreshing --
        # the chapters of the book you just left do not apply to this one.
        self.documents.currentIndexChanged.connect(self._on_document_changed)
        self.locate_button.clicked.connect(self.locate_document)
        self.edit_button.clicked.connect(self._edit_selected)
        self.retire_button.clicked.connect(lambda: self._set_active(False))
        self.restore_button.clicked.connect(lambda: self._set_active(True))
        self.star_button.clicked.connect(self._toggle_star)
        self.purge_button.clicked.connect(self._purge)
        self.export_button.clicked.connect(self._export)
        self.import_button.clicked.connect(self._import)
        self.remove_button.clicked.connect(self._remove_document)

        self.reload_documents()
        self.reload_chapters()
        self.refresh()

    # --- loading ------------------------------------------------------------

    @property
    def document_id(self) -> int | None:
        """The chosen document, or None for all of them."""
        return self.documents.currentData()

    def reload_documents(self, select_id: int | None = None) -> None:
        """Fills the document picker, keeping the current choice where it can.

        "All documents" is offered first and is the default, so a multi-document
        bank starts on everything. With exactly one document that entry is
        dropped and the document itself is chosen, which is where this tab parts
        company with Quiz and Progress -- they hide their picker and choose
        nothing.

        The difference is not an oversight. `Remove…` and `Locate PDF…` both act
        on one named document and are disabled without one, so a bank holding a
        single book would be unable to delete it or find its PDF. The other two
        tabs offer neither, so they have nothing to name.
        """
        wanted = select_id if select_id is not None else self.document_id
        documents = repo.list_documents(self.connection)

        self.documents.blockSignals(True)
        self.documents.clear()
        if len(documents) != 1:
            self.documents.addItem(self.tr("All documents"), None)
        for row in documents:
            missing = "" if Path(row["path"]).exists() else self.tr("  (file missing)")
            self.documents.addItem(f"{row['filename']}{missing}", row["id"])
        if wanted is not None:
            index = self.documents.findData(wanted)
            if index >= 0:
                self.documents.setCurrentIndex(index)
        self.documents.blockSignals(False)
        self._update_locate()

    def reload_chapters(self) -> None:
        """The chapters of the chosen document, or of all of them."""
        self.chapter_filter.blockSignals(True)
        previous = self.chapter_filter.currentData()
        self.chapter_filter.clear()
        self.chapter_filter.addItem(self.tr("Any chapter"), None)
        for row in repo.list_chapters(self.connection, self.document_id):
            self.chapter_filter.addItem(row["title"], row["title"])
        if previous is not None:
            # Survives a document change only if the new one has that chapter
            # too; otherwise the filter falls back to "Any chapter" rather than
            # silently showing nothing.
            index = self.chapter_filter.findData(previous)
            if index >= 0:
                self.chapter_filter.setCurrentIndex(index)
        self.chapter_filter.blockSignals(False)

    @Slot()
    def _on_document_changed(self) -> None:
        self.reload_chapters()
        self._update_locate()
        self.refresh()

    def _update_locate(self) -> None:
        """Offers to find the PDF only when one document is chosen and it is gone."""
        document_id = self.document_id
        self.locate_button.setVisible(
            document_id is not None
            and not repo.document_is_present(self.connection, document_id))
        # Removing needs to know which document is meant, and "All documents"
        # does not say.
        self.remove_button.setEnabled(document_id is not None)

    @Slot()
    def refresh(self) -> None:
        chosen_type = self.type_filter.currentData()
        document_id = self.document_id
        # Taken before the reload and restored after it. Resetting a model drops
        # the selection, so starring a row left nothing selected and the next
        # click had to find it again -- by id, because row numbers do not
        # survive a filter change.
        keep = set(self.model.ids_at(self._selected_rows()))
        self.model.reload(
            text=self.search.text().strip() or None,
            types=(chosen_type,) if chosen_type else None,
            chapter=self.chapter_filter.currentData(),
            state=self.state.currentData() or "active",
            starred_only=self.starred_only.isChecked(),
            document_id=document_id,
        )
        shown = self.model.rowCount()
        # Both counted within the chosen document, so "12 shown of 182" cannot
        # mean twelve of one book against the total of two.
        total = len(repo.search_questions(self.connection, state="active",
                                          document_id=document_id, limit=100_000))
        review = len(repo.search_questions(self.connection, state="review",
                                           document_id=document_id))
        self._label_review(review)

        parts = [self.tr("{0} shown · {1} active in the bank").format(shown, total)]
        if not shown and total:
            # An empty table beside a non-empty bank looks like a fault. Say
            # which it is.
            parts.append(self.tr("nothing matches this filter"))
        elif review and self.state.currentData() != "review":
            parts.append(self.tr("{0} worth reviewing").format(review))
        self.count.setText("  ·  ".join(parts))
        self._reselect(keep)

    def _reselect(self, ids: set[int]) -> None:
        """Puts the selection back on the rows that are still on screen.

        Silently drops the ones a filter has just hidden, which is the only
        sensible reading: a question that no longer matches is not selected, and
        saying so with a dialog would be worse than saying nothing.
        """
        if not ids:
            return
        selection = self.table.selectionModel()
        model = self.model
        flags = (QItemSelectionModel.Select | QItemSelectionModel.Rows)
        selection.blockSignals(True)
        for row in range(model.rowCount()):
            if model.row_at(row)["id"] in ids:
                selection.select(model.index(row, 0), flags)
        selection.blockSignals(False)

    def _label_review(self, review: int) -> None:
        """Puts the count on the review filter, so it says whether to bother."""
        index = self.state.findData("review")
        if index >= 0:
            self.state.setItemText(
                index,
                self.tr("Needs review ({0})").format(review) if review
                else self.tr("Needs review"))

    def _selected_rows(self) -> list[int]:
        return sorted(index.row() for index in
                      self.table.selectionModel().selectedRows())

    # --- actions ------------------------------------------------------------

    @Slot()
    def _edit_selected(self) -> None:
        rows = self._selected_rows()
        if rows:
            self._open_editor(self.model.row_at(rows[0]))

    @Slot(QModelIndex)
    def _edit_row(self, index) -> None:
        if index.isValid():
            self._open_editor(self.model.row_at(index.row()))

    def _open_editor(self, row) -> None:
        dialog = EditDialog(row, self)
        while dialog.exec() == QDialog.Accepted:
            problem = dialog.problem()
            if problem:
                QMessageBox.warning(self, self.tr("Cannot save"), problem)
                continue
            # `edited_at` is stamped by repo.update_question, which is what stops
            # a later regeneration of this chunk throwing the edit away.
            repo.update_question(self.connection, row["id"], **dialog.changes())
            self.refresh()
            self.changed.emit()
            return

    def _set_active(self, active: bool) -> None:
        ids = self.model.ids_at(self._selected_rows())
        if not ids:
            return
        repo.set_active(self.connection, ids, active)
        self.refresh()
        self.changed.emit()

    @Slot()
    def _toggle_star(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        # Judged by the first selected row, so a mixed selection ends up all the
        # same rather than each flipping to its own opposite.
        target = not self.model.row_at(rows[0])["starred"]
        repo.set_starred_many(self.connection, self.model.ids_at(rows), target)
        self.refresh()
        # No `changed` here, unlike every other write on this tab. A star shows
        # nowhere else: the Quiz offers "Starred" whether or not anything is,
        # Progress counts no stars, and nothing is added or removed for the
        # quiz's open session to notice. Announcing it would refresh four views
        # to redraw one cell.

    @Slot()
    def _purge(self) -> None:
        """Deletes retired questions that have no answers behind them."""
        purgeable = repo.purgeable_count(self.connection)
        retired = repo.bank_totals(self.connection)["retired"]
        if not purgeable:
            QMessageBox.information(
                self, self.tr("Purge retired"),
                self.tr("Nothing can be purged. All {0} retired questions have "
                        "answers recorded against them, and deleting those would "
                        "take part of the history with them.").format(retired))
            return

        answer = QMessageBox.question(
            self, self.tr("Purge retired"),
            self.tr("Delete {0} retired questions permanently?\n\n"
                    "{1} more are retired but have answers recorded against them; "
                    "those are kept.").format(purgeable, retired - purgeable),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            repo.purge_inactive(self.connection)
            self.refresh()
            self.changed.emit()

    @Slot()
    def locate_document(self) -> None:
        """Points the chosen document at the same PDF somewhere else.

        The path is absolute and recorded at import, so a bank whose folder was
        renamed -- or that was opened on another machine -- knows the questions
        but not where to find the pages behind them.

        The file offered has to be the same one, checked by hash. A different
        edition would keep every stored page number while the text underneath had
        moved, and the reveal would land confidently on the wrong paragraph.
        """
        document_id = self.document_id
        if document_id is None:
            return
        document = repo.get_document(self.connection, document_id)
        if document is None:
            return

        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Find {0}").format(document["filename"]), "",
            self.tr("PDF (*.pdf)"))
        if not path:
            return

        if repo.relocate_document(self.connection, document_id, path):
            self.reload_documents(select_id=document_id)
            self.refresh()
            self.count.setText(self.tr("Found it. The page previews work again."))
            # The Generate tab re-renders the blurred pages when it hears this,
            # and the quiz picks the new path up for any session already open.
            self.document_relocated.emit(document_id)
            self.changed.emit()
        else:
            QMessageBox.warning(
                self, self.tr("Not the same file"),
                self.tr("That PDF is not the one these questions came from -- its "
                        "contents differ.\n\nEvery question stores a page number and "
                        "a position on that page. Against a different edition they "
                        "would point at the wrong text, so the file is not accepted."))

    # --- moving a bank between machines -------------------------------------

    @Slot()
    def _export(self) -> None:
        """Writes the chosen document's questions out as one JSON file.

        Whatever the picker says is what travels, so "All documents" exports the
        whole bank and a single book exports just that book.
        """
        document_id = self.document_id
        if document_id is None:
            documents = repo.list_documents(self.connection)
            if not documents:
                QMessageBox.information(
                    self, self.tr("Export"),
                    self.tr("There is nothing in the bank to export yet."))
                return
            suggested = "studyapp-bank.json"
        else:
            document = repo.get_document(self.connection, document_id)
            if document is None:
                return
            suggested = f"{Path(document['filename']).stem}.json"

        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Export questions"), suggested, self.tr("JSON (*.json)"))
        if not path:
            return

        try:
            written = transfer.write(self.connection, path, document_id)
        except OSError as error:
            QMessageBox.warning(self, self.tr("Export failed"), str(error))
            return

        if not written["questions"]:
            QMessageBox.information(
                self, self.tr("Export"),
                self.tr("That document has no questions, so the file is empty."))
            return

        self.count.setText(
            self.tr("Exported {0} questions from {1} document(s) to {2}")
            .format(written["questions"], written["documents"], Path(path).name))

    @Slot()
    def _import(self) -> None:
        """Reads an export in, adding only what this bank does not already have.

        The file is read and described before anything is written, so the
        confirmation can say what is about to happen rather than asking for
        permission in the abstract.
        """
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Import questions"), "", self.tr("JSON (*.json)"))
        if not path:
            return

        try:
            payload = transfer.read(path)
        except transfer.TransferError as error:
            QMessageBox.warning(self, self.tr("Cannot read that file"), str(error))
            return

        summary = transfer.describe(payload)
        if not summary["questions"]:
            QMessageBox.information(
                self, self.tr("Import"),
                self.tr("That export has no questions in it."))
            return

        names = "\n".join(f"  · {name}" for name in summary["filenames"][:6])
        if len(summary["filenames"]) > 6:
            names += self.tr("\n  · and {0} more").format(
                len(summary["filenames"]) - 6)
        answer = QMessageBox.question(
            self, self.tr("Import questions"),
            self.tr("{0} questions from {1} document(s):\n\n{2}\n\n"
                    "Questions already in this bank are skipped, so importing "
                    "the same file twice changes nothing. Nothing is deleted.")
            .format(summary["questions"], summary["documents"], names),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer != QMessageBox.Yes:
            return

        try:
            report = transfer.apply(self.connection, payload)
        except sqlite3.Error as error:
            QMessageBox.warning(self, self.tr("Import failed"), str(error))
            return

        self.reload_documents()
        self.reload_chapters()
        self.refresh()
        self.changed.emit()

        lines = [self.tr("Added {0} questions.").format(report.questions_added)]
        if report.questions_skipped:
            lines.append(self.tr("{0} were already here and were skipped.")
                         .format(report.questions_skipped))
        if report.needs_pdf:
            # The questions are in; their pages are not, until the file is found.
            lines.append("")
            lines.append(self.tr(
                "These need their PDF before the source pane can show anything. "
                "Choose the document above and click Locate PDF:"))
            lines.extend(f"  · {name}" for name in report.needs_pdf)
        QMessageBox.information(self, self.tr("Imported"), "\n".join(lines))

    @Slot()
    def _remove_document(self) -> None:
        """Deletes a document and everything that came from it.

        The hard one. `Remove…` on the Generate tab unloads: chunks that produced
        questions are archived and the bank is untouched, because that tab is
        about what is left to generate. This is the other thing, and the one the
        bank needs -- the document, its chunks, its questions and the answers
        recorded against them, gone.

        Which is why the dialog counts them out loud before asking, and why it
        says plainly that the PDF on disk is not touched: "remove the PDF" is
        the obvious reading of the button, and it is the wrong one.
        """
        document_id = self.document_id
        if document_id is None:
            return
        document = repo.get_document(self.connection, document_id)
        if document is None:
            return

        history = repo.document_history(self.connection, document_id)
        lines = [
            self.tr("Remove {0} from the bank?").format(document["filename"]),
            "",
            self.tr("This deletes {0} questions and {1} recorded answers, and "
                    "cannot be undone.").format(history["questions"],
                                                history["attempts"]),
            "",
            self.tr("The PDF file itself is left alone, only what StudyApp "
                    "generated from it goes."),
        ]
        confirm = QMessageBox(self)
        confirm.setWindowTitle(self.tr("Remove document"))
        confirm.setText("\n".join(lines))
        confirm.setIcon(QMessageBox.Warning)
        remove = confirm.addButton(self.tr("Remove"), QMessageBox.DestructiveRole)
        confirm.addButton(self.tr("Cancel"), QMessageBox.RejectRole)
        confirm.setDefaultButton(confirm.buttons()[-1])
        confirm.exec()
        if confirm.clickedButton() is not remove:
            return

        sha, pages = document["sha256"], document["page_count"]
        repo.delete_document(self.connection, document_id)
        # The blurred pages are keyed by hash and nothing points at them now.
        freed = blur.clear_cache(sha)

        self.reload_documents()
        self.reload_chapters()
        self.refresh()
        self.changed.emit()
        self.count.setText(
            self.tr("Removed {0} — {1} questions and {2} page previews with it.")
            .format(document["filename"], history["questions"], freed or pages))
