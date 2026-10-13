"""Table models over rows that repo.py hands out.

Written by hand rather than with `QSqlTableModel`, which would be shorter. The
reason is that QtSql opens its own connection to the same file, and two
connections into one SQLite database is a second set of PRAGMAs to remember and
a stale-cache problem after every write. Sixty lines is a fair price for staying
on one connection and one access path.
"""

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from . import style

# What each chunk status looks like in the table. Failure is the only one that
# gets colour: everything else is a normal stage of a run, and colouring them all
# would leave nothing standing out when something actually breaks.
STATUS_COLORS = {
    "failed": style.WRONG_TEXT,
    "done": style.INK,
    "running": style.INK_SECONDARY,
    "skipped": style.INK_TERTIARY,
    "pending": style.INK_TERTIARY,
}


class ChunkTableModel(QAbstractTableModel):
    """The chunks of one document, with how each one's generation went."""

    COLUMNS = ("#", "Pages", "Chapter", "Status", "Questions")
    ORDINAL, PAGES, TITLE, STATUS, QUESTIONS = range(5)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list = []

    # --- Qt interface -------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.tr(self.COLUMNS[section])
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = index.column()

        if role == Qt.DisplayRole:
            if column == self.ORDINAL:
                return row["ordinal"]
            if column == self.PAGES:
                # Stored 0-based to match PyMuPDF and QtPdf; shown 1-based
                # because that is what is printed on the page.
                return f"{row['page_start'] + 1}–{row['page_end'] + 1}"
            if column == self.TITLE:
                return row["title"] or "—"
            if column == self.STATUS:
                return row["status"]
            if column == self.QUESTIONS:
                return row["question_count"] or ""

        if role == Qt.ForegroundRole and column == self.STATUS:
            from PySide6.QtGui import QColor

            return QColor(STATUS_COLORS.get(row["status"], style.INK))

        if role == Qt.ToolTipRole:
            # The failure reason has nowhere else to go, and it is the one thing
            # you want when a row says "failed".
            if row["error"]:
                return row["error"]
            return row["title"] or None

        if role == Qt.TextAlignmentRole and column in (self.ORDINAL, self.QUESTIONS):
            return int(Qt.AlignRight | Qt.AlignVCenter)

        return None

    # --- loading ------------------------------------------------------------

    def set_rows(self, rows) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def chunk_id(self, row: int) -> int:
        return int(self._rows[row]["id"])

    def row_for_chunk(self, chunk_id: int) -> int | None:
        for position, row in enumerate(self._rows):
            if row["id"] == chunk_id:
                return position
        return None

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self._rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return counts

    def question_total(self) -> int:
        return sum(row["question_count"] or 0 for row in self._rows)


class Column:
    """One column of a `RowsModel`: a heading and how to get the cell out of a row."""

    def __init__(self, heading: str, key, align_right: bool = False, tooltip=None):
        self.heading = heading
        # A string reads the row by that name; a callable computes the cell,
        # which is what the derived columns -- accuracy, a percentage -- need.
        self.key = key
        self.align_right = align_right
        self.tooltip = tooltip

    def value(self, row):
        return self.key(row) if callable(self.key) else row[self.key]


class RowsModel(QAbstractTableModel):
    """A read-only table over rows from repo.py, described by its columns.

    The statistics tables differ only in their columns, so they share this rather
    than each carrying its own copy of the same six Qt methods.
    """

    def __init__(self, columns: list[Column], parent=None):
        super().__init__(parent)
        self.columns = columns
        self._rows: list = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.tr(self.columns[section].heading)
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = self.columns[index.column()]

        if role == Qt.DisplayRole:
            return column.value(row)
        if role == Qt.TextAlignmentRole and column.align_right:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ToolTipRole and column.tooltip:
            return column.tooltip(row)
        return None

    def set_rows(self, rows) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, position: int):
        return self._rows[position]
