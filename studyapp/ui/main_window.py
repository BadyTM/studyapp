"""The window: two tabs, one for practising and one for generating.

The split is not cosmetic. Generating needs the network, a subscription or an API
key and a progress bar; practising needs none of the three and is what the app is
open for 99% of the time. Keeping them apart is what lets the packaged build
eventually drop the generator entirely.

The window owns the one database connection and the controller that writes
through it. No logic lives in this package -- widgets and signals only.
"""

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..db import connection as db_connection
from ..generate.worker import GenerationController
from . import style
from .bank_view import BankView
from .generate_view import GenerateView
from .quiz_view import QuizView
from .stats_view import StatsView


class PlaceholderView(QWidget):
    """A tab that is not built yet, saying so rather than showing an empty box."""

    def __init__(self, title: str, detail: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("canvas")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(10)
        layout.addStretch()

        heading = QLabel(title)
        heading.setObjectName("question")
        heading.setAlignment(Qt.AlignCenter)
        layout.addWidget(heading)

        note = QLabel(detail)
        note.setObjectName("meta")
        note.setAlignment(Qt.AlignCenter)
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addStretch()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(self.tr("StudyApp"))
        self.setMinimumSize(940, 620)

        # One connection, opened here and used by everything. The controller
        # writes through it from this thread and no other.
        self.connection = db_connection.connect()
        self.controller = GenerationController(self.connection, self)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.quiz_view = QuizView(self.connection, self)
        self.tabs.addTab(self.quiz_view, self.tr("Quiz"))
        self.stats_view = StatsView(self.connection, self)
        self.tabs.addTab(self.stats_view, self.tr("Progress"))
        self.bank_view = BankView(self.connection, self)
        self.tabs.addTab(self.bank_view, self.tr("Question bank"))
        self.generate_view = GenerateView(self.connection, self.controller, self)
        self.tabs.addTab(self.generate_view, self.tr("Generate"))
        self.setCentralWidget(self.tabs)

        # Every tab reads the same bank, and any of the others may have changed
        # it. Re-reading on the way in is cheaper than keeping four views in
        # step, and there is only ever one of them on screen.
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.bank_view.changed.connect(self.quiz_view.start_screen.refresh)
        # Removing a document happens on the bank tab, and the Generate tab was
        # left holding it: the picker still listed it, every action on it failed
        # quietly because the row was gone, and switching tabs did not help
        # because nothing refreshed this one.
        self.bank_view.changed.connect(self.generate_view.reload_documents)
        # Removing or importing a document changes which books Progress
        # can report on, and the figures for the one it is showing.
        self.bank_view.changed.connect(self.stats_view.refresh)
        self.generate_view.changed.connect(self.stats_view.refresh)
        # And a session already running on the Quiz tab may be holding questions
        # that have just been deleted. It cannot be finished; it has to stop.
        self.bank_view.changed.connect(self.quiz_view.bank_changed)
        self.generate_view.changed.connect(self.quiz_view.start_screen.refresh)
        # Relocating the PDF happens on the Generate tab; a session already open
        # on the Quiz tab has to hear about it.
        self.generate_view.changed.connect(self.quiz_view.document_moved)
        # Resetting empties the mistakes queue, which the start screen offers.
        self.stats_view.changed.connect(self.quiz_view.start_screen.refresh)
        self.stats_view.changed.connect(self.bank_view.refresh)
        self.generate_view.changed.connect(self.bank_view.reload_chapters)
        # Importing or removing a PDF changes what the bank can be filtered by.
        self.generate_view.changed.connect(self.bank_view.reload_documents)
        # Finding a moved PDF happens on the bank tab; re-rendering its blurred
        # pages needs the controller, which the Generate tab owns.
        self.bank_view.document_relocated.connect(self.generate_view.ensure_blur)
        self.bank_view.document_relocated.connect(self.quiz_view.document_moved)

        # A session open on a document whose blurred pages were still being
        # rendered is showing "preview not ready". It does not have to wait for
        # the next question to find out that it is.
        self.controller.blur_ready.connect(self.quiz_view.preview_ready)

        self.controller.status.connect(self.statusBar().showMessage)
        self.controller.failed.connect(self._show_failure)
        self.statusBar().showMessage(self.tr("Database: {0}").format(config.db_path()))

        self._settings = QSettings(config.ORG_NAME, config.APP_NAME)
        self._restore_geometry()

    def _restore_geometry(self) -> None:
        geometry = self._settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    def _on_tab_changed(self, index: int) -> None:
        current = self.tabs.widget(index)
        if current is self.quiz_view:
            self.quiz_view.start_screen.refresh()
        elif current is self.stats_view:
            self.stats_view.refresh()
        elif current is self.bank_view:
            self.bank_view.reload_chapters()
            self.bank_view.refresh()
        elif current is self.generate_view:
            # The one tab that had no branch here, which is how a document
            # deleted elsewhere survived on it until the window was reopened.
            self.generate_view.reload_documents()

    def _show_failure(self, message: str) -> None:
        QMessageBox.warning(self, self.tr("StudyApp"), message)

    def closeEvent(self, event) -> None:
        # Saved on the way out rather than on every resize: a hundred writes per
        # drag buys nothing, and this is the only moment the size is final.
        self._settings.setValue("window/geometry", self.saveGeometry())
        self.quiz_view.save_state()

        # Everything in flight is asked to stop and given a moment to land --
        # the blur render as well as any generation, or the window would wait on
        # it. Nothing is half-written: a chunk is recorded only once its
        # questions are, and the rest go back to `pending`.
        self.controller.shutdown(5_000)
        self.connection.close()
        super().closeEvent(event)


def create_window() -> MainWindow:
    window = MainWindow()
    window.setStyleSheet(style.stylesheet())
    return window
