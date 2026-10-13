"""The source pane: the book, on screen the whole time, unreadable until it isn't.

Two layers in a `QStackedWidget`. Layer 0 is a pre-rendered blurred page as a
plain image; layer 1 is the real `QPdfView`. Revealing is a switch between them,
which is instant and cannot flicker -- unlike a `QGraphicsBlurEffect` over a
scrolling view, which repaints the blur on every scroll step and looks it.

The marker over the quote is drawn by highlight.py, not by Qt: `QPdfView` paints
its own search results in a hardcoded cyan that cannot be themed and is
unreadable over text. Drawing it ourselves also means using the rectangles
computed during generation, which are exact where a live search is approximate.

What the blurred state must not give away is more than the words. A page number
or a chapter title in the strip above answers the question by itself, so both are
withheld until the reveal, and the blurred layer deliberately shows *the previous
question's page* rather than the one coming. The jump that arrives with the
reveal then covers the difference in scale between the two layers -- and, more to
the point, means watching the pane tells you nothing before you have answered.
"""

from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QVBoxLayout,
)

from ..ingest import blur
from .highlight import HighlightOverlay


class BlurredPage(QLabel):
    """The out-of-focus layer. Just an image, scaled to the pane."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(200, 200)
        self.setScaledContents(False)
        self._pixmap: QPixmap | None = None

    def set_page(self, path: Path | None) -> None:
        if path is None or not path.exists():
            self.show_message(self.tr("preview not ready"))
            return
        self._pixmap = QPixmap(str(path))
        self.setText("")
        self._rescale()

    def show_message(self, text: str) -> None:
        """Words instead of a page, for when there is no page to show."""
        self._pixmap = None
        self.setPixmap(QPixmap())
        self.setWordWrap(True)
        self.setText(text)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            return
        self.setPixmap(self._pixmap.scaled(
            self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))


class SourcePane(QFrame):
    """The right-hand card: metadata strip on top, page below."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("previewCard")

        self._document_sha: str | None = None
        self._pdf_path: str | None = None
        # Where the pane is currently parked. The blurred layer keeps showing
        # this while the next question is being answered.
        self._resting_page = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(12)

        meta = QHBoxLayout()
        self.chapter_label = QLabel(" ")
        self.chapter_label.setObjectName("meta")
        self.page_label = QLabel(self.tr("locked"))
        self.page_label.setObjectName("meta")
        self.page_label.setAlignment(Qt.AlignRight)
        meta.addWidget(self.chapter_label, 1)
        meta.addWidget(self.page_label)
        layout.addLayout(meta)

        self.stack = QStackedWidget()
        self.blurred = BlurredPage()
        self.document = QPdfDocument(self)

        self.viewer = QPdfView()
        self.viewer.setDocument(self.document)
        self.viewer.setPageMode(QPdfView.PageMode.MultiPage)
        self.viewer.setZoomMode(QPdfView.ZoomMode.FitToWidth)

        # No QPdfSearchModel: Qt paints its results in a hardcoded cyan that is
        # unreadable over the text and cannot be themed. The marker is drawn on
        # this overlay instead, from the rectangles worked out at generation
        # time -- which are exact, where a live search is approximate.
        self.marks = HighlightOverlay(self.viewer, self.document)
        self.viewer.viewport().installEventFilter(self)

        self.stack.addWidget(self.blurred)      # 0 -- locked
        self.stack.addWidget(self.viewer)       # 1 -- revealed
        layout.addWidget(self.stack, 1)

    # --- loading ------------------------------------------------------------

    def set_document(self, pdf_path: str | None, document_sha: str | None) -> None:
        """Points the pane at a PDF. Cheap to call again with the same one.

        The hash is part of what identifies it, not just the path. A PDF edited
        and re-imported keeps its path but gets a new hash and a new set of
        blurred pages -- comparing paths alone would go on serving the previous
        edition's pages from behind the blur.
        """
        if pdf_path == self._pdf_path and document_sha == self._document_sha:
            return
        self._pdf_path = pdf_path
        self._document_sha = document_sha
        self._resting_page = 0
        if pdf_path and Path(pdf_path).exists():
            self.document.load(pdf_path)

    @property
    def document_ready(self) -> bool:
        """Whether there is a PDF to show at all.

        Worth asking before revealing: the path was recorded at import and the
        file may have been moved, renamed or deleted since. Qt loads nothing and
        says nothing, so the pane would go blank while the strip above it still
        announced a page number.
        """
        return self.document.status() == QPdfDocument.Status.Ready

    # --- the two states -----------------------------------------------------

    def lock(self) -> None:
        """Blur the pane and take the metadata away.

        Called before every question. The page shown is the one the pane was
        already on -- never the one the answer is on, which is not known to this
        widget until `reveal` is called.
        """
        self.chapter_label.setText(" ")
        self.page_label.setText(self.tr("locked"))
        self.marks.clear()
        if self._document_sha:
            self.blurred.set_page(blur.page_path(self._document_sha, self._resting_page))
        else:
            self.blurred.set_page(None)
        self.stack.setCurrentIndex(0)

    def reveal(self, page: int | None, quote: str, chapter: str | None,
               page_count: int = 0, rects=None) -> None:
        """Bring the source into focus, on the right page, quote highlighted.

        The page number comes from the database, worked out at generation time by
        finding the quote in the PDF -- so the jump lands correctly even in the
        cases where Qt's own search cannot match the text.
        """
        self.chapter_label.setText(chapter or " ")

        # Only when a file was named and could not be opened. A pane that was
        # never given a document has nothing missing -- saying "not found" about
        # a path nobody supplied would be nonsense.
        if self._pdf_path and not self.document_ready:
            # The question, its answer and its quote are all still on the left,
            # so the session carries on -- only the page is gone.
            self.page_label.setText(self.tr("source file not found"))
            self.blurred.show_message(self.tr(
                "The PDF this question came from is not where it was imported "
                "from:\n\n{0}\n\nMove it back, or import it again.")
                .format(self._pdf_path or "?"))
            self.stack.setCurrentIndex(0)
            self.marks.clear()
            return

        if page is None:
            # The quote was never located in the PDF. Say so rather than show a
            # page that is probably not the right one.
            self.page_label.setText(self.tr("source not located"))
        else:
            self.page_label.setText(
                self.tr("p. {0} / {1}").format(page + 1, page_count or "?"))
            navigator = self.viewer.pageNavigator()
            if navigator is not None:
                navigator.jump(page, QPointF(0, 0))
            self._resting_page = page

        self.stack.setCurrentIndex(1)
        # The pane may have been resized while it was locked, and the
        # viewer only lays out when it becomes visible again.
        self._fit_overlay()
        self.marks.set_marks(page, rects)
        # After the layer is up, so the viewport has its real size to scroll in.
        self.marks.scroll_to_marks()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_overlay()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_overlay()

    def eventFilter(self, watched, event) -> bool:
        """Refits the marker when the viewer resizes, not only when this pane does.

        `resizeEvent` here fires before the QPdfView inside the stack has been
        laid out, so the overlay was sized against the viewport's previous
        geometry. Watching the viewport itself closes the gap -- it is the thing
        the marker is drawn over, so it is the thing to follow.
        """
        if watched is self.viewer.viewport() and event.type() == QEvent.Resize:
            self._fit_overlay()
        return super().eventFilter(watched, event)

    def _fit_overlay(self) -> None:
        """Keeps the marker layer exactly over the viewport it draws on."""
        self.marks.setGeometry(self.viewer.viewport().rect())
        self.marks.raise_()

    def highlight_found(self) -> bool:
        """Whether there is a marker to draw at all."""
        return bool(self.marks.marker_rects())

    @property
    def is_revealed(self) -> bool:
        return self.stack.currentIndex() == 1
