"""The stylesheet.

Qt fails at this silently in both directions: an unparseable stylesheet is
dropped whole, and a control left unstyled quietly keeps the native look. Both
produce a running app that simply looks wrong, so the checks here are about
which rules exist rather than about pixels.
"""

import pytest
from PySide6.QtWidgets import QCheckBox, QComboBox, QRadioButton, QTableView

from studyapp.ui import style


@pytest.fixture(scope="module")
def qss(qapp) -> str:
    return style.stylesheet()


class TestSelection:
    def test_rows_are_selected_in_the_paper_palette(self, qss):
        # The bug this fixes: with only selection-background-color set on the
        # view, the native style painted the cells in the Windows accent colour
        # and did it as slivers at the cell edges rather than a filled row.
        assert "QTableView::item:selected" in qss
        assert style.SELECTION in qss

    def test_the_selection_colour_belongs_to_the_palette(self):
        # Warm grey, a shade of the canvas -- not a system accent.
        red, green, blue = (int(style.SELECTION[i:i + 2], 16) for i in (1, 3, 5))
        assert red > blue, "the selection should stay on the warm side"
        assert abs(red - green) < 20 and abs(green - blue) < 20

    def test_the_focus_rectangle_is_off(self, qss):
        # Otherwise Qt outlines the current cell on top of the row highlight.
        assert "outline: 0" in qss


class TestIndicators:
    def test_a_checked_radio_is_filled_with_ink(self, qss):
        # Unstyled, the checked state was invisible on this ground -- there was
        # no telling which question type was chosen.
        assert "QRadioButton::indicator:checked" in qss
        section = qss[qss.index("QRadioButton::indicator:checked"):][:160]
        assert style.INK in section

    def test_checkboxes_match(self, qss):
        assert "QCheckBox::indicator:checked" in qss

    def test_radios_and_checkboxes_are_told_apart_by_shape(self, qss):
        # A round radio and a square checkbox; only the corner radius says which.
        assert "QRadioButton::indicator {" in qss
        assert "QCheckBox::indicator {" in qss


class TestScrollbars:
    def test_the_stepper_arrows_are_removed(self, qss):
        assert "QScrollBar::add-line" in qss and "QScrollBar::sub-line" in qss

    def test_the_track_is_empty(self, qss):
        section = qss[qss.index("QScrollBar::add-page"):][:120]
        assert "transparent" in section

    def test_the_handle_is_thin_and_rounded(self, qss):
        section = qss[qss.index("QScrollBar::handle:vertical"):][:200]
        assert "border-radius" in section


class TestChevrons:
    def test_one_is_drawn_for_each_direction(self, qapp):
        down = style.chevron("down")
        up = style.chevron("up")
        assert down != up
        from pathlib import Path

        assert Path(down).exists() and Path(up).exists()

    def test_the_path_uses_forward_slashes(self, qapp):
        # Qt stylesheets take forward slashes on every platform, including this
        # one; a backslash makes the rule fail without saying so.
        assert "\\" not in style.chevron("down")

    def test_drawing_it_twice_returns_the_same_file(self, qapp):
        assert style.chevron("down") == style.chevron("down")

    def test_the_stylesheet_points_at_them(self, qss):
        assert "QComboBox::down-arrow" in qss
        assert "QSpinBox::up-arrow" in qss
        assert style.chevron("down") in qss


class TestRowHover:
    """Hover has to cover the row, not the cell under the cursor.

    Qt marks only that one cell with State_MouseOver, so a stylesheet can only
    ever light the cell -- which reads as a stray patch rather than as "this is
    the row you are pointing at".
    """

    @pytest.fixture
    def table(self, qapp):
        from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt as QtNs

        from studyapp.ui.tables import HoverTableView

        class Rows(QAbstractTableModel):
            def rowCount(self, parent=QModelIndex()):
                return 0 if parent.isValid() else 5

            def columnCount(self, parent=QModelIndex()):
                return 0 if parent.isValid() else 3

            def data(self, index, role=QtNs.DisplayRole):
                if role == QtNs.DisplayRole:
                    return f"r{index.row()}c{index.column()}"
                return None

        view = HoverTableView()
        view.setModel(Rows())
        view.resize(400, 300)
        yield view
        view.deleteLater()

    def test_nothing_is_hovered_to_begin_with(self, table):
        assert table.hovered_row == -1

    def test_mouse_tracking_is_on(self, table):
        # Off, Qt sends move events only while a button is held, and the hover
        # would follow the mouse once per drag instead of continuously.
        assert table.hasMouseTracking()

    def test_leaving_the_table_clears_it(self, table):
        from PySide6.QtCore import QEvent

        table._set_hovered(2)
        assert table.hovered_row == 2
        table.leaveEvent(QEvent(QEvent.Leave))
        assert table.hovered_row == -1

    def test_the_stylesheet_no_longer_lights_single_cells(self, qss):
        # The per-cell rule has to be gone, or it would paint one cell of the
        # hovered row differently from the rest of it.
        assert "QTableView::item:hover" not in qss

    def test_hover_is_lighter_than_selection(self):
        def brightness(colour):
            return sum(int(colour[i:i + 2], 16) for i in (1, 3, 5))

        assert brightness(style.HOVER) > brightness(style.SELECTION)

    def test_every_table_in_the_app_has_it(self, qapp, tmp_path):
        # One shared class, so a table added later gets this for free -- and a
        # table that went back to a plain QTableView would show up here.
        from studyapp.db import connection as db_connection
        from studyapp.generate.worker import GenerationController
        from studyapp.ui.bank_view import BankView
        from studyapp.ui.generate_view import GenerateView
        from studyapp.ui.stats_view import StatsView
        from studyapp.ui.tables import HoverTableView

        path = tmp_path / "hover.db"
        db_connection.ensure_schema(path)
        connection = db_connection.connect(path)

        bank = BankView(connection)
        stats = StatsView(connection)
        generate = GenerateView(connection, GenerationController(connection))
        for table in (bank.table, generate.table, stats.chapter_table):
            assert isinstance(table, HoverTableView)
        connection.close()


class TestItApplies:
    def test_qt_accepts_the_whole_stylesheet(self, qapp, qss):
        # Qt drops a stylesheet it cannot parse, in full and in silence, so the
        # only proof it took is reading it back off the application.
        qapp.setStyleSheet(qss)
        assert len(qapp.styleSheet()) == len(qss)

    def test_the_styled_widgets_polish_without_complaint(self, qapp, qss):
        qapp.setStyleSheet(qss)
        for widget_type in (QRadioButton, QCheckBox, QComboBox, QTableView):
            widget = widget_type()
            widget.ensurePolished()
            widget.deleteLater()
