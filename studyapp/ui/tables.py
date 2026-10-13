"""Two things Qt has no notion of: a hovered row, and a filter under a column.

`QTableView::item:hover` is a rule about one item, so a stylesheet paints the
single cell the mouse is over and the row it belongs to stays plain -- which
reads as a stray patch rather than as "this is the row you are pointing at", and
is worse than no hover at all. So the row is tracked by the view and painted by a
delegate.

A filter row is the same kind of gap. Qt's header draws text and nothing else;
there is no place to put a widget in it. `FilterHeader` is therefore a plain
widget parked in the space between the header and the first row, with its
children moved to follow the columns.
"""

from PySide6.QtCore import QModelIndex, QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHeaderView,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QWidget,
)

from . import style


class RowHoverDelegate(QStyledItemDelegate):
    """Paints the hovered row behind the cells, before they draw themselves."""

    def paint(self, painter, option, index: QModelIndex) -> None:
        table = self.parent()
        hovered = getattr(table, "hovered_row", -1)

        # A selected row already has a background, and painting hover over it
        # would make the selection change shade as the mouse crossed it.
        is_selected = bool(option.state & QStyle.State_Selected)
        if index.row() == hovered and not is_selected:
            painter.fillRect(option.rect, QColor(style.HOVER))

        # Qt sets this on the one cell under the cursor. Left in place, the
        # style would paint that cell differently from the rest of its row.
        option.state &= ~QStyle.State_MouseOver
        super().paint(painter, option, index)


class HoverTableView(QTableView):
    """A read-only table with row-wide hover, in the shape every view wants."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hovered_row = -1
        # Created only if a view asks for filters, so the two tables that do not
        # want them are laid out exactly as they were.
        self._filters: "FilterHeader | None" = None
        # updateGeometries widens the viewport margin and Qt answers by calling
        # updateGeometries again. Without this the two take turns forever.
        self._laying_out = False
        # Without this Qt sends move events only while a button is held, and the
        # hover would follow the mouse just once per drag.
        self.setMouseTracking(True)
        self.setItemDelegate(RowHoverDelegate(self))

        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setHighlightSections(False)

    def _set_hovered(self, row: int) -> None:
        if row == self.hovered_row:
            return
        self.hovered_row = row
        # The whole viewport, because the row that is losing the highlight has
        # to be repainted as well as the one gaining it.
        self.viewport().update()

    def mouseMoveEvent(self, event) -> None:
        self._set_hovered(self.rowAt(int(event.position().y())))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._set_hovered(-1)
        super().leaveEvent(event)

    def stretch_column(self, column: int) -> None:
        """Sizes every column to its contents, and lets one take the slack."""
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(column, QHeaderView.Stretch)

    # --- filters under the headings -----------------------------------------

    @property
    def filters(self) -> "FilterHeader":
        """The filter row, made on first use.

        Asking for it is what turns it on: the table then keeps a strip of empty
        space under the headings and moves the controls to follow the columns.
        """
        if self._filters is None:
            self._filters = FilterHeader(self)
            header = self.horizontalHeader()
            header.sectionResized.connect(lambda *_: self._filters.reposition())
            header.sectionMoved.connect(lambda *_: self._filters.reposition())
            self.horizontalScrollBar().valueChanged.connect(
                lambda *_: self._filters.reposition())
            self.updateGeometries()
        return self._filters

    def updateGeometries(self) -> None:
        """Reserves the strip under the header, then puts the filters in it.

        Qt sets the top viewport margin to the header's height here; the filter
        row lives in the extra margin added on top of that, which is the one
        place a widget can sit between the headings and the first row.
        """
        if self._laying_out:
            return
        self._laying_out = True
        try:
            super().updateGeometries()
            if self._filters is None:
                return
            extra = self._filters.sizeHint().height()
            margins = self.viewportMargins()
            self.setViewportMargins(margins.left(), margins.top() + extra,
                                    margins.right(), margins.bottom())
            self._filters.setGeometry(margins.left(), margins.top(),
                                      self.viewport().width(), extra)
            self._filters.reposition()
        finally:
            self._laying_out = False


class FilterHeader(QWidget):
    """A row of controls under the column headings, each one under its column.

    Put here rather than above the table because that is what it is about: the
    combo under "Type" filters by type, and no label is needed to say so. The
    search box under "Question" is the same idea -- it used to carry a
    placeholder explaining which fields it searched.

    Columns that have no filter simply have no widget; the gap is the answer.

    Created through `HoverTableView.filters`, never directly, because it only
    works in a table that has made room for it.
    """

    PADDING = 4                 # breathing room above and below the controls
    GAP = 3                     # so two neighbouring combos do not touch

    def __init__(self, table: "HoverTableView"):
        super().__init__(table)
        self.setObjectName("filterHeader")
        self.table = table
        self._widgets: dict[int, QWidget] = {}

    def set_widget(self, column: int, widget: QWidget, width: int | None = None) -> None:
        """Puts a control under one column, optionally fixing that column's width.

        `width` exists because the column and its filter disagree about how wide
        they want to be, and the column always won: "Type" is sized to the word
        `mcq`, which left its combo box clipped to an arrow and half a letter.
        A column given a width is taken off `ResizeToContents` -- it is now sized
        for the control, not the content -- and never goes narrower than the
        control needs.

        Leave it None for a column that is already wide enough, or one that
        stretches.
        """
        widget.setParent(self)
        widget.show()
        self._widgets[column] = widget

        if isinstance(widget, QComboBox):
            # A combo asks to be as wide as its longest entry, which for the
            # chapter filter was 878 pixels -- the width of a chapter title, in
            # a column that shows them truncated anyway. Here the column decides
            # how wide the control is, not the other way round.
            widget.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            widget.setMinimumContentsLength(6)

        if width is not None:
            header = self.table.horizontalHeader()
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            header.resizeSection(column, max(width, widget.sizeHint().width()
                                             + 2 * self.GAP))
        self.reposition()

    def paintEvent(self, event) -> None:
        """The strip's own ground, and the hairline that closes it off.

        Two different mechanisms, because only one of them works. A bare QWidget
        ignores its stylesheet entirely; drawing it through the style brings the
        background back, but not the border -- `PE_Widget` paints the fill and
        stops there, so a `border-bottom` rule is accepted and silently ignored.

        That line is not decoration. Without it the filters sit directly against
        the first row of questions and read as one of them, which is exactly the
        confusion the strip was moved up here to avoid. So it is drawn by hand,
        in the card's border tone -- a step darker than the header's own
        hairline, because this one has to close off a filled strip.
        """
        from PySide6.QtGui import QPainter
        from PySide6.QtWidgets import QStyleOption

        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PE_Widget, option, painter, self)

        painter.setPen(QColor(style.CARD_BORDER))
        bottom = self.height() - 1
        painter.drawLine(0, bottom, self.width(), bottom)

    def widget(self, column: int) -> QWidget | None:
        return self._widgets.get(column)

    def sizeHint(self) -> QSize:
        tallest = max((widget.sizeHint().height()
                       for widget in self._widgets.values()), default=0)
        return QSize(0, tallest + 2 * self.PADDING if tallest else 0)

    def reposition(self) -> None:
        """Moves every control over its column.

        Called whenever the columns can have moved: a section resized or
        dragged, a horizontal scroll, or the table itself laid out again. The
        positions come from the header in viewport coordinates, which is already
        scroll-aware, so scrolling needs no arithmetic of its own.
        """
        header = self.table.horizontalHeader()
        height = max(self.height() - 2 * self.PADDING, 0)
        for column, widget in self._widgets.items():
            left = header.sectionViewportPosition(column)
            width = header.sectionSize(column)

            if isinstance(widget, QCheckBox):
                # A box has a fixed size, so filling the column would only park
                # it against the left edge. Centred under the heading instead.
                box = widget.sizeHint().width()
                widget.setGeometry(left + (width - box) // 2, self.PADDING,
                                   box, height)
                widget.setVisible(width >= box)
                continue

            # A column narrower than its control gets a clipped one rather than
            # a control spilling over its neighbour.
            widget.setGeometry(left + self.GAP, self.PADDING,
                               max(width - 2 * self.GAP, 0), height)
            # Qt keeps hidden sections at zero width; a zero-wide widget still
            # paints a border, so hide it outright.
            widget.setVisible(width > 2 * self.GAP)
