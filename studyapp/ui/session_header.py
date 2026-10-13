"""The strip above the question: one mark per question in the session.

Drawn in a single `paintEvent` rather than as twenty small widgets. Twenty
widgets would be twenty layout entries to keep in step and twenty repaints to
trigger; one widget redraws on a single `update()` and is less code besides.

Two rules the design rests on, both easy to break by accident:

  * **The marks are not clickable.** Jumping to another question would go around
    the rule that Reveal is the only way forward. A tooltip is as far as it goes.
  * **The current question stays neutral until it is answered.** Colouring it
    ahead of time would leak the result into the one place the reader looks
    while they are still thinking.
"""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..quiz.session import Outcome
from . import style

DOT_WIDTH = 11
DOT_HEIGHT = 4
DOT_GAP = 4
DOT_RADIUS = 2

# Past this many, the marks no longer fit beside the label at a sensible card
# width. Shrinking them below ~8px makes them unreadable, so the header changes
# shape instead: a hairline and a count.
MAX_DOTS = 30

BAR_HEIGHT = 2
BAR_MAX_WIDTH = 180


class SessionHeader(QWidget):
    """Progress through the session, as marks or as a hairline."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(20)
        self._outcomes: list[Outcome] = []
        self._current = 0
        self._label = ""

    def set_session(self, outcomes, current: int, label: str) -> None:
        self._outcomes = list(outcomes)
        self._current = current
        self._label = label
        self.setToolTip("")
        self.update()

    # --- painting -----------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        metrics = painter.fontMetrics()
        text = self._right_text()
        text_width = metrics.horizontalAdvance(text)
        baseline = (self.height() + metrics.ascent() - metrics.descent()) / 2

        painter.setPen(QColor(style.INK_META))
        painter.drawText(int(self.width() - text_width), int(baseline), text)
        painter.setPen(Qt.NoPen)

        if not self._outcomes:
            return
        if len(self._outcomes) <= MAX_DOTS:
            self._paint_dots(painter)
        else:
            self._paint_bar(painter)

    def _paint_dots(self, painter: QPainter) -> None:
        top = (self.height() - DOT_HEIGHT) / 2
        for position, outcome in enumerate(self._outcomes):
            left = position * (DOT_WIDTH + DOT_GAP)
            painter.setBrush(QColor(self._dot_color(position, outcome)))
            painter.drawRoundedRect(
                QRectF(left, top, DOT_WIDTH, DOT_HEIGHT), DOT_RADIUS, DOT_RADIUS)

    def _dot_color(self, position: int, outcome: Outcome) -> str:
        if outcome is Outcome.CORRECT:
            return style.DOT_CORRECT
        if outcome is Outcome.WRONG:
            return style.DOT_WRONG
        # Neutral ink for where we are, blank for what is still ahead. The
        # current mark never takes a result colour before there is a result.
        return style.DOT_CURRENT if position == self._current else style.DOT_FUTURE

    def _paint_bar(self, painter: QPainter) -> None:
        width = min(BAR_MAX_WIDTH, self.width() // 3)
        top = (self.height() - BAR_HEIGHT) / 2
        painter.setBrush(QColor(style.DOT_FUTURE))
        painter.drawRect(QRectF(0, top, width, BAR_HEIGHT))

        done = sum(1 for outcome in self._outcomes if outcome is not Outcome.UNANSWERED)
        fraction = done / len(self._outcomes) if self._outcomes else 0
        painter.setBrush(QColor(style.INK_META))
        painter.drawRect(QRectF(0, top, width * fraction, BAR_HEIGHT))

    def _right_text(self) -> str:
        if not self._outcomes:
            return self._label
        return f"{min(self._current + 1, len(self._outcomes))} / " \
               f"{len(self._outcomes)} · {self._label}"
