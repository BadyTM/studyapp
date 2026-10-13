"""The yellow marker over the quoted sentence.

Qt paints its own search results in a hardcoded cyan -- not themeable, not
readable over black text, and nothing in `QPdfView`'s API or palette changes it.
So the marker is drawn here instead, as a transparent layer over the viewport,
in the paper-and-ink palette the rest of the app uses.

The rectangles are not searched for at display time: they were worked out during
generation, by finding the quote in the PDF with PyMuPDF, and stored on the
question. That pass is what also produced the page number, so the two can never
disagree -- and drawing from stored coordinates is exact where a live text search
is approximate, particularly for a sentence broken across a line.

What this file has to get right is the transform from PDF page points to pixels
in the viewport, which `QPdfView` does not expose. It is reconstructed from the
parts it does expose, and checked against where Qt itself draws.
"""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from . import style

# Painted under the text rather than over it, so the words stay black. The alpha
# is what keeps it looking like a marker pen rather than a filled box.
MARKER = QColor(style.HIGHLIGHT)
MARKER_ALPHA = 165

# A marker drawn exactly on the glyph boxes looks mean; a little air makes it
# read as a stroke across the line.
PADDING_X = 1.0
PADDING_Y = 2.0
RADIUS = 2.0


class HighlightOverlay(QWidget):
    """A transparent sheet over the PDF viewport that draws the marker."""

    def __init__(self, viewer, document, parent=None):
        super().__init__(parent or viewer.viewport())
        self.viewer = viewer
        self.document = document
        self._page: int | None = None
        self._rects: list[list[float]] = []
        # Purely decorative: clicks, scrolling and selection all belong to the
        # view underneath.
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)

        viewer.verticalScrollBar().valueChanged.connect(self.update)
        viewer.horizontalScrollBar().valueChanged.connect(self.update)
        viewer.zoomFactorChanged.connect(self.update)
        self.raise_()

    # --- what to draw -------------------------------------------------------

    def set_marks(self, page: int | None, rects) -> None:
        self._page = page
        self._rects = [list(rect) for rect in (rects or [])]
        self.update()

    def clear(self) -> None:
        self.set_marks(None, [])

    # --- the transform ------------------------------------------------------

    def scale(self) -> float:
        """Points to pixels.

        In fit-to-width the page is scaled so its width fills the viewport less
        the document margins; `zoomFactor` multiplies that in the other modes.
        """
        if self._page is None:
            return 1.0
        width = self.document.pagePointSize(self._page).width()
        if width <= 0:
            return 1.0
        margins = self.viewer.documentMargins()
        available = self.viewer.viewport().width() - margins.left() - margins.right()
        if self.viewer.zoomMode() == self.viewer.ZoomMode.FitToWidth:
            return available / width
        return self.viewer.zoomFactor()

    def page_top(self, scale: float) -> float:
        """Where the top of the current page sits in the scrolled content.

        Pages are stacked with a fixed gap, so the offset is the height of
        everything above it. Cached per page because this runs on every repaint
        and page 115 would otherwise measure 115 pages first.
        """
        if self._page is None:
            return 0.0
        key = (self._page, round(scale, 4))
        if getattr(self, "_top_key", None) == key:
            return self._top_value

        margins = self.viewer.documentMargins()
        spacing = self.viewer.pageSpacing()
        top = float(margins.top())
        for number in range(self._page):
            top += self.document.pagePointSize(number).height() * scale + spacing

        self._top_key, self._top_value = key, top
        return top

    def viewport_rect(self, rect: list[float], scale: float, top: float) -> QRectF:
        """One stored rectangle, in viewport pixels."""
        x0, y0, x1, y1 = rect
        margins = self.viewer.documentMargins()
        left = margins.left() + x0 * scale - self.viewer.horizontalScrollBar().value()
        upper = top + y0 * scale - self.viewer.verticalScrollBar().value()
        return QRectF(left - PADDING_X, upper - PADDING_Y,
                      (x1 - x0) * scale + 2 * PADDING_X,
                      (y1 - y0) * scale + 2 * PADDING_Y)

    def marker_rects(self) -> list[QRectF]:
        if self._page is None or not self._rects:
            return []
        scale = self.scale()
        top = self.page_top(scale)
        return [self.viewport_rect(rect, scale, top) for rect in self._rects]

    def scroll_to_marks(self) -> None:
        """Puts the marked sentence a third of the way down the viewport.

        Jumping to the top of the page leaves the reader hunting for the line;
        a third down keeps the sentence in its context, with what precedes it
        still visible.
        """
        if self._page is None or not self._rects:
            return
        scale = self.scale()
        top = self.page_top(scale)
        target = top + min(rect[1] for rect in self._rects) * scale
        bar = self.viewer.verticalScrollBar()
        bar.setValue(max(0, int(target - self.viewer.viewport().height() / 3)))

    # --- painting -----------------------------------------------------------

    def paintEvent(self, event) -> None:
        rects = self.marker_rects()
        if not rects:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        colour = QColor(MARKER)
        colour.setAlpha(MARKER_ALPHA)
        painter.setBrush(colour)
        # Multiply keeps the glyphs black instead of washing them out, which is
        # what a real marker does to text on paper.
        painter.setCompositionMode(QPainter.CompositionMode_Multiply)
        for rect in rects:
            painter.drawRoundedRect(rect, RADIUS, RADIUS)
