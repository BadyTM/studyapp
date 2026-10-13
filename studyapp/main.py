"""Starts the application."""

import sys
import traceback

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from . import config
from .db import connection as db_connection
from .ui import cursors
from .ui.main_window import create_window


def set_icon(application: QApplication) -> None:
    """Puts the book on the window and on the taskbar button.

    The taskbar needs the extra step: Windows groups buttons by an application
    id, and with none set the window is filed under pythonw.exe and shows
    Python's icon there no matter what the window carries.
    """
    icon = config.icon_path()
    if icon.exists():
        application.setWindowIcon(QIcon(str(icon)))

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                config.APP_ID)
        except Exception:
            pass        # cosmetic only; never worth failing to start over


def install_excepthook(parent_for_dialog=None) -> None:
    """Makes an exception inside a Qt slot visible.

    Qt does not let an exception out of a slot: it prints the traceback to
    stderr and the event loop carries on. Under pythonw.exe -- which is how this
    app is started -- stderr goes nowhere, so the reader clicks a control, the
    control does nothing, and there is no sign anything happened.

    One dialog per problem, not per occurrence: a slot that raises on every
    repaint would otherwise bury the screen in message boxes.
    """
    seen: set[str] = set()

    def report(kind, value, trace) -> None:
        if kind is KeyboardInterrupt:
            sys.__excepthook__(kind, value, trace)
            return
        text = "".join(traceback.format_exception(kind, value, trace))
        sys.stderr.write(text)
        signature = f"{kind.__name__}:{value}"
        if signature in seen:
            return
        seen.add(signature)
        try:
            QMessageBox.warning(
                None, "StudyApp",
                "Something went wrong:\n\n"
                f"{kind.__name__}: {value}\n\n"
                "The app is still running, but that action did not finish.")
        except Exception:                   # noqa: BLE001 - never mask the first
            pass

    sys.excepthook = report


def main(argv: list[str] | None = None) -> int:
    application = QApplication(argv if argv is not None else sys.argv)
    install_excepthook()
    application.setApplicationName(config.APP_NAME)
    application.setOrganizationName(config.ORG_NAME)
    set_icon(application)
    # Kept on the application so it outlives this function; an event filter is
    # not owned by what it watches.
    application._cursors = cursors.install(application)

    # Cheap when there is nothing to do, and it means the window never opens onto
    # a database a migration has not been applied to.
    db_connection.ensure_schema()

    window = create_window()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
