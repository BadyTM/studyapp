"""A pointing hand over everything that can be clicked.

Qt stylesheets have no `cursor` property -- it is accepted and ignored, which is
why this is not one line in style.py. The cursor has to be set on each widget,
and setting it on each widget by hand means every dialog and every control added
later starts out wrong again.

So it is done by one filter on the application: whenever a widget is polished,
if it is the kind of thing you click, it gets the hand.

The second half matters as much as the first. A disabled widget keeps the cursor
it was given -- Qt does not clear it -- so a greyed-out button would still invite
a click it will not accept. The filter therefore also watches for the enabled
state changing, and takes the cursor away again.
"""

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractButton, QComboBox, QTabBar, QWidget

# Anything that responds to a click. QAbstractButton covers push buttons, radio
# buttons, check boxes and tool buttons in one.
CLICKABLE = (QAbstractButton, QComboBox)

# The quiz's answer options are frames, not buttons, so they are recognised by
# name -- see quiz_view.OptionButton and the QSS that styles it.
CLICKABLE_NAMES = {"option"}


def is_clickable(widget: QWidget) -> bool:
    return isinstance(widget, CLICKABLE) or widget.objectName() in CLICKABLE_NAMES


def apply_to(widget: QWidget) -> None:
    """Gives a widget the hand, or takes it back if it cannot be used."""
    if widget.isEnabled():
        widget.setCursor(Qt.PointingHandCursor)
    else:
        # Not "arrow": unset lets it inherit whatever the parent uses, which is
        # what a widget with no cursor of its own would have shown anyway.
        widget.unsetCursor()


class PointerCursors(QObject):
    """Installed on the application, once, at startup."""

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()

        # A tab bar is one widget spanning the whole strip, so setting the
        # cursor on it would put a hand over the empty space beside the last
        # tab as well. It gets decided per position instead.
        if kind == QEvent.MouseMove and isinstance(watched, QTabBar):
            tab = watched.tabAt(event.position().toPoint())
            if tab >= 0 and watched.isTabEnabled(tab):
                watched.setCursor(Qt.PointingHandCursor)
            else:
                watched.unsetCursor()
            return False

        if kind in (QEvent.Polish, QEvent.EnabledChange):
            if isinstance(watched, QWidget) and is_clickable(watched):
                apply_to(watched)
        return False


def install(application) -> PointerCursors:
    """Starts the filter and returns it, so the caller can keep it alive.

    The application does not own an event filter, so dropping this on the floor
    would have it collected and the cursors would quietly stop appearing.
    """
    filter_object = PointerCursors(application)
    application.installEventFilter(filter_object)
    return filter_object
