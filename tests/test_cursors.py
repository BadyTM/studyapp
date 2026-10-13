"""The pointing hand over clickable controls.

Two things here are easy to get wrong and invisible in a screenshot: Qt's
stylesheet accepts `cursor` and ignores it, so a rule that looks right does
nothing; and a disabled widget keeps the cursor it was given, so a greyed-out
button goes on inviting a click it will not accept.
"""

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QComboBox,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QTabBar,
    QWidget,
)

from studyapp.ui import cursors


@pytest.fixture
def filtered(qapp):
    """The application with the filter installed, removed again afterwards."""
    installed = cursors.install(qapp)
    yield qapp
    qapp.removeEventFilter(installed)


class TestStylesheetCannotDoThis:
    def test_qt_ignores_the_cursor_property(self, qapp):
        # The reason this is a filter and not a line in style.py. Qt parses the
        # rule, keeps the rest of the stylesheet, and drops this one silently.
        qapp.setStyleSheet("QPushButton { cursor: pointer; }")
        button = QPushButton("x")
        button.ensurePolished()
        assert button.cursor().shape() == Qt.ArrowCursor
        qapp.setStyleSheet("")


class TestWhatCounts:
    @pytest.mark.parametrize("widget_type", [QPushButton, QRadioButton,
                                             QCheckBox, QComboBox])
    def test_clickable_things_are_recognised(self, qapp, widget_type):
        assert cursors.is_clickable(widget_type())

    def test_a_text_field_is_not(self, qapp):
        # It has its own I-beam, which says something true about it.
        assert not cursors.is_clickable(QLineEdit())

    def test_a_plain_widget_is_not(self, qapp):
        assert not cursors.is_clickable(QWidget())

    def test_the_quiz_options_are(self, qapp):
        # They are frames rather than buttons, so they are known by name.
        frame = QWidget()
        frame.setObjectName("option")
        assert cursors.is_clickable(frame)


class TestApplying:
    def test_an_enabled_control_gets_the_hand(self, qapp):
        button = QPushButton("x")
        cursors.apply_to(button)
        assert button.cursor().shape() == Qt.PointingHandCursor

    def test_a_disabled_one_gives_it_back(self, qapp):
        # Qt does not clear the cursor when a widget is disabled, so this has to.
        button = QPushButton("x")
        cursors.apply_to(button)
        button.setEnabled(False)
        cursors.apply_to(button)
        assert button.cursor().shape() != Qt.PointingHandCursor

    def test_it_follows_the_enabled_state_both_ways(self, filtered):
        button = QPushButton("x")
        button.ensurePolished()
        assert button.cursor().shape() == Qt.PointingHandCursor

        button.setEnabled(False)
        filtered.processEvents()
        assert button.cursor().shape() == Qt.ArrowCursor

        button.setEnabled(True)
        filtered.processEvents()
        assert button.cursor().shape() == Qt.PointingHandCursor


class TestTabBar:
    def test_the_hand_appears_over_a_tab_and_not_beside_it(self, filtered):
        # The bar is one widget spanning the strip; setting the cursor on the
        # whole thing would put a hand over the empty space after the last tab.
        bar = QTabBar()
        # A bare QTabBar stretches its tabs across the whole width, which would
        # leave no empty space to test against. The app's bar does not expand --
        # see the gap after "Generate" -- so the test matches that.
        bar.setExpanding(False)
        bar.addTab("one")
        bar.addTab("two")
        bar.resize(400, 30)

        def move(point):
            QApplication.sendEvent(bar, QMouseEvent(
                QEvent.MouseMove, QPointF(point), bar.mapToGlobal(point),
                Qt.NoButton, Qt.NoButton, Qt.NoModifier))
            return bar.cursor().shape()

        assert move(bar.tabRect(1).center()) == Qt.PointingHandCursor
        assert move(QPoint(bar.width() - 8, 15)) == Qt.ArrowCursor


class TestAcrossTheApp:
    def test_every_enabled_control_in_the_window_has_it(self, filtered, tmp_path):
        from studyapp.db import connection as db_connection
        from studyapp.ui.main_window import create_window

        path = tmp_path / "cursors.db"
        db_connection.ensure_schema(path)

        import os

        previous = os.environ.get("STUDYAPP_DB")
        os.environ["STUDYAPP_DB"] = str(path)
        try:
            window = create_window()
            window.show()
            filtered.processEvents()

            controls = [w for w in window.findChildren(QAbstractButton)
                        + window.findChildren(QComboBox) if w.isEnabled()]
            missing = [w for w in controls
                       if w.cursor().shape() != Qt.PointingHandCursor]
            assert controls, "the window should have controls to check"
            assert not missing, f"{len(missing)} controls without the hand"
            window.close()
        finally:
            if previous is None:
                os.environ.pop("STUDYAPP_DB", None)
            else:
                os.environ["STUDYAPP_DB"] = previous

    def test_a_dialog_opened_later_gets_it_too(self, filtered):
        # The filter is on the application rather than on each window, so
        # anything built afterwards is covered without being told.
        dialog = QWidget()
        button = QPushButton("x", dialog)
        dialog.show()
        filtered.processEvents()
        assert button.cursor().shape() == Qt.PointingHandCursor
        dialog.close()
