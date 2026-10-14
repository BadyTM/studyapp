"""Shared fixtures.

Qt is set to the offscreen platform before anything imports it, so the widget
tests run without opening windows -- on a machine with no display as much as on
this one.

The other thing settled here is where the suite is allowed to write. Left alone
it writes where the app does, which is not merely untidy: the blurred-page cache
is keyed by document hash and checked by whether the file exists, so a run leaves
state behind that the *next* run reads as work already done. A test that renders
a cache and then asserts something about it passes on the second run no matter
what the code does.
"""

import gc
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def isolated_data_dir(tmp_path_factory):
    """Points the app's data directory at a temporary one, for the whole run.

    Everything derived from it follows: the database, the blurred pages, the
    drawn chevrons and the CLI's session folder. Nothing a test does can reach
    the reader's own bank or leave anything in their cache.
    """
    from studyapp import config

    root = tmp_path_factory.mktemp("studyapp-data")
    real = config.data_dir
    config.data_dir = lambda: root
    yield root
    config.data_dir = real


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole session -- Qt allows no more than one.

    Taken down deliberately, because the tests leave windows behind. A test that
    builds three views in order to look at them has no reason to close them, and
    nothing else does either, so they outlive this fixture: the QApplication is
    destroyed first and the widgets after it, which is a segfault inside Qt long
    after the last assertion passed.

    It cost a red CI run to find, and the way it failed is worth remembering.
    The suite printed "689 passed" and the process then exited non-zero with
    nothing said about why -- the crash happens during interpreter shutdown,
    after pytest has written its summary and returned 0. The exit code is
    whatever the OS makes of it: 1 on the runner, 139 and 127 here depending on
    which file ran. A trimmed PySide6 happened not to crash at all, which is why
    this passed locally for weeks.
    """
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    yield application

    for widget in application.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    application.processEvents()     # deleteLater is a promise, not a deletion
    gc.collect()
