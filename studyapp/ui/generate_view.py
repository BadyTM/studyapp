"""The Generate tab: import a PDF, run the generation, watch it go.

Widgets and signals only. Every decision this view appears to make is actually
made by `GenerationController`, which also owns the one connection that writes.

The tab exists mainly so a run can be watched and resumed. It is used once per
book and then rarely again -- which is exactly why it has to survive being closed
half way through, and why the chunk table is the main thing on it.
"""

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..db import repo
from ..ingest import blur
from .models import ChunkTableModel
from .tables import HoverTableView


class GenerateView(QWidget):
    """Import and generation for one document at a time."""

    changed = Signal()                  # the bank moved under the other tabs

    def __init__(self, connection, controller, parent=None):
        super().__init__(parent)
        self.setObjectName("canvas")
        self.setAcceptDrops(True)

        self.connection = connection
        self.controller = controller
        self.document_id: int | None = None

        self._build()
        self._connect()
        self.reload_documents()

    # --- layout -------------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.documents = QComboBox()
        self.documents.setMinimumWidth(280)
        top.addWidget(self.documents, 1)

        self.remove_button = QPushButton(self.tr("Remove…"))
        self.remove_button.setObjectName("ghost")
        top.addWidget(self.remove_button)

        self.import_button = QPushButton(self.tr("Import PDF…"))
        self.import_button.setObjectName("ghost")
        top.addWidget(self.import_button)
        layout.addLayout(top)

        self.hint = QLabel(self.tr("or drop a PDF anywhere on this tab"))
        self.hint.setObjectName("hint")
        layout.addWidget(self.hint)

        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(10)

        self.summary = QLabel("—")
        self.summary.setObjectName("meta")
        card_layout.addWidget(self.summary)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMaximum(1)
        self.progress.setValue(0)
        # Qt ignores `height` in the stylesheet for a progress bar, so without
        # this it renders at its default size -- a full-width grey slab that
        # reads as a broken widget rather than the design's hairline.
        self.progress.setFixedHeight(2)
        # Hidden when nothing is running: an empty bar sitting at zero is not
        # information, it is furniture.
        self.progress.setVisible(False)
        card_layout.addWidget(self.progress)

        self.table = HoverTableView()
        self.table.setModel(ChunkTableModel(self))
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.stretch_column(ChunkTableModel.TITLE)
        card_layout.addWidget(self.table, 1)
        layout.addWidget(card, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.start_button = QPushButton(self.tr("Start"))
        self.start_button.setObjectName("primary")
        self.stop_button = QPushButton(self.tr("Stop"))
        self.stop_button.setObjectName("ghost")
        self.stop_button.setEnabled(False)
        self.regenerate_button = QPushButton(self.tr("Regenerate selected"))
        self.regenerate_button.setObjectName("ghost")
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(self.regenerate_button)
        buttons.addStretch()
        self.cost = QLabel("")
        self.cost.setObjectName("meta")
        buttons.addWidget(self.cost)
        layout.addLayout(buttons)

    def _connect(self) -> None:
        self.import_button.clicked.connect(self.choose_pdf)
        self.remove_button.clicked.connect(self.remove_document)
        self.documents.currentIndexChanged.connect(self._on_document_changed)
        self.start_button.clicked.connect(self._on_start)
        self.stop_button.clicked.connect(self.controller.stop)
        self.regenerate_button.clicked.connect(self._on_regenerate)

        self.controller.progress.connect(self._on_progress)
        self.controller.chunk_done.connect(self._on_chunk_done)
        self.controller.finished.connect(self._on_finished)
        self.controller.imported.connect(self._on_imported)
        self.controller.status.connect(self.cost.setText)
        self.controller.failed.connect(self._on_failed)

    # --- drag and drop ------------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if self._dropped_pdf(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        # Guarded like the Import button beside it, which is disabled during a
        # run. The drop path was not, so a PDF dropped mid-run could fail its
        # import, and the failure -- arriving on the same signal a generation
        # failure does -- unlocked the buttons underneath a run that was still
        # going. Stop greyed out, Start refused to start, and the run could no
        # longer be cancelled at all.
        if self.controller.is_running:
            self.summary.setText(
                self.tr("A run is going. Stop it before importing another PDF."))
            return
        path = self._dropped_pdf(event)
        if path:
            event.acceptProposedAction()
            self.controller.import_pdf(path)

    @staticmethod
    def _dropped_pdf(event) -> str | None:
        data = event.mimeData()
        if not data.hasUrls():
            return None
        for url in data.urls():
            path = url.toLocalFile()
            if path.lower().endswith(".pdf"):
                return path
        return None

    # --- documents ----------------------------------------------------------

    @Slot()
    def choose_pdf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Import PDF"), "", self.tr("PDF (*.pdf)"))
        if path:
            self.controller.import_pdf(path)

    @Slot(int)
    def ensure_blur(self, document_id: int) -> None:
        """Re-renders a document's blurred pages if they are not on disk.

        Called when the Question bank tab has just pointed a document at its PDF
        again: the bank can fix the path, but the render needs the thread pool
        and the controller, which live here. A bank sent to someone else arrives
        with no blur cache at all, so this is also the moment it gets built.
        """
        document = repo.get_document(self.connection, document_id)
        if document is None:
            return
        if not blur.is_complete(document["sha256"], document["page_count"]):
            self.controller.prerender_blur(
                document["path"], document["sha256"], document["page_count"])
        self.reload_documents(select_id=document_id)

    @Slot()
    def remove_document(self) -> None:
        """Takes the selected document out of the generation list.

        Chunks that produced questions are archived rather than deleted, because
        the questions read their chapter, page, highlight rectangles and the PDF
        through them -- deleting the chunk would leave the bank full of cards
        with an empty source pane. Chunks that produced nothing are deleted.

        So the question bank is never touched here, and the dialog says which of
        the two is about to happen rather than making the reader guess.
        """
        if self.document_id is None:
            return
        document = repo.get_document(self.connection, self.document_id)
        if document is None:
            return

        plan = repo.unload_preview(self.connection, self.document_id)
        lines = [self.tr("Remove {0} from the list?").format(document["filename"]), ""]
        if plan["deleted"]:
            lines.append(self.tr("{0} chunks with no questions will be deleted.")
                         .format(plan["deleted"]))
        if plan["archived"]:
            lines.append(
                self.tr("{0} chunks are kept, archived, because {1} questions were "
                        "generated from them. Those questions stay in the bank and "
                        "keep their page previews.")
                .format(plan["archived"], plan["questions"]))
        if not plan["deleted"] and not plan["archived"]:
            lines.append(self.tr("There is nothing left to remove."))
        lines.append("")
        lines.append(self.tr("No questions are deleted. The PDF itself is not touched."))

        answer = QMessageBox.question(
            self, self.tr("Remove document"), "\n".join(lines),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return

        sha = document["sha256"]
        outcome = repo.unload_document(self.connection, self.document_id)
        # The blurred pages go only when the document does. While any chunk
        # survives, its questions still need them to draw the source pane.
        freed = blur.clear_cache(sha) if outcome["document_deleted"] else 0

        self.document_id = None
        self.reload_documents()
        self.changed.emit()

        if outcome["archived"]:
            self.cost.setText(
                self.tr("Removed {0} · {1} chunks archived, questions kept")
                .format(document["filename"], outcome["archived"]))
        else:
            self.cost.setText(
                self.tr("Removed {0} · {1} cached pages freed")
                .format(document["filename"], freed))

    def reload_documents(self, select_id: int | None = None) -> None:
        """Rebuilds the picker, keeping the document that is open where it can.

        Called whenever the bank changes as well as after an import, and most of
        those changes have nothing to do with this tab -- starring a question
        must not move the reader to a different book. So without an explicit
        `select_id` the current document is kept, and falling back to the first
        one is what happens when it is genuinely gone.
        """
        wanted = select_id if select_id is not None else self.document_id
        documents = repo.list_documents(self.connection, generatable_only=True)
        self.documents.blockSignals(True)
        self.documents.clear()
        for row in documents:
            self.documents.addItem(
                f"{row['filename']}  ·  {row['page_count']} pages", row["id"])
        self.documents.blockSignals(False)

        if not documents:
            self.document_id = None
            self.table.model().set_rows([])
            self.summary.setText(self.tr("No document imported yet."))
            self._update_buttons()
            return

        index = self.documents.findData(wanted) if wanted is not None else -1
        self.documents.setCurrentIndex(max(0, index))
        self._on_document_changed()

    @Slot()
    def _on_document_changed(self) -> None:
        self.document_id = self.documents.currentData()
        self.refresh_chunks()

    def refresh_chunks(self) -> None:
        if self.document_id is None:
            return
        rows = repo.list_chunks(self.connection, self.document_id)
        self.table.model().set_rows(rows)
        self._update_summary()
        self._update_buttons()

    def _update_summary(self) -> None:
        model = self.table.model()
        counts = model.status_counts()
        pending = counts.get("pending", 0) + counts.get("running", 0)
        parts = [
            self.tr("{0} chunks").format(model.rowCount()),
            self.tr("{0} pending").format(pending),
            self.tr("{0} done").format(counts.get("done", 0)),
        ]
        if counts.get("skipped"):
            parts.append(self.tr("{0} skipped").format(counts["skipped"]))
        if counts.get("failed"):
            parts.append(self.tr("{0} failed").format(counts["failed"]))
        parts.append(self.tr("{0} questions").format(model.question_total()))
        self.summary.setText("  ·  ".join(parts))

    def _update_buttons(self, running: bool | None = None) -> None:
        """Enables what can be used right now.

        Two separate conditions, and they used to be one boolean: whether a run
        is going, and whether there is a document to run on. Conflating them
        disabled Import on an empty database -- the one moment it is the only
        control that can help.
        """
        running = self.controller.is_running if running is None else running
        has_document = self.document_id is not None

        # Importing is blocked only while a run is going, never for want of a
        # document: that is the reason to import one.
        self.import_button.setEnabled(not running)
        self.remove_button.setEnabled(not running and has_document)
        self.start_button.setEnabled(not running and has_document)
        self.regenerate_button.setEnabled(not running and has_document)
        self.stop_button.setEnabled(running)

    # --- running ------------------------------------------------------------

    @Slot()
    def _on_start(self) -> None:
        if self.document_id is None:
            return
        self._update_buttons(running=True)
        self.controller.start(self.document_id)

    @Slot()
    def _on_regenerate(self) -> None:
        """Regenerates the selected chunks -- queues them and runs them.

        It used to only queue, and leave you to press Start. The button says
        "Regenerate", so it regenerates; a button that marks a row and waits for
        another button looks like nothing happened.

        Only the selected chunks run, not everything else that happens to be
        pending. Hand-edited questions are safe either way: the controller
        retires only the ones nobody has touched when the new batch arrives.
        """
        if self.controller.is_running or self.document_id is None:
            return
        model = self.table.model()
        chunk_ids = {model.chunk_id(index.row())
                     for index in self.table.selectionModel().selectedRows()}
        if not chunk_ids:
            return

        self.controller.requeue(chunk_ids)
        self.refresh_chunks()
        self._update_buttons(running=True)
        self.controller.start(self.document_id, chunk_ids=chunk_ids)

    @Slot(int, int)
    def _on_progress(self, completed: int, total: int) -> None:
        self.progress.setVisible(True)
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(completed)

    @Slot(int, str, int)
    def _on_chunk_done(self, chunk_id: int, status: str, written: int) -> None:
        # Reloading the whole table on every chunk is fine at this size and
        # keeps the counts honest without a second source of truth.
        self.refresh_chunks()
        row = self.table.model().row_for_chunk(chunk_id)
        if row is not None:
            self.table.scrollTo(self.table.model().index(row, 0))

    @Slot()
    def _on_finished(self) -> None:
        self._update_buttons(running=False)
        self.progress.setVisible(False)
        self.refresh_chunks()

    @Slot(int, bool)
    def _on_imported(self, document_id: int, is_new: bool) -> None:
        self.reload_documents(select_id=document_id)

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        """Shows a failure from any job: a run, an import, a blur render.

        The buttons are reset from the controller's own state, not forced to
        "not running". An import that failed while a run was going used to
        unlock everything and leave the run uncancellable.
        """
        self.summary.setText(message)
        self._update_buttons()
