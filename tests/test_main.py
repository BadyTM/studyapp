"""Starting the app, and what happens when something inside it goes wrong.

Qt does not let an exception out of a slot: it prints the traceback to stderr
and the event loop carries on. Under pythonw.exe -- which is how this app is
started -- stderr goes nowhere, so a control that raises simply does nothing,
with no sign that anything happened at all.
"""

import sys

import pytest

from studyapp import main


@pytest.fixture
def restore_excepthook():
    original = sys.excepthook
    yield
    sys.excepthook = original


class TestTheExcepthook:
    def test_installing_it_replaces_the_default(self, restore_excepthook):
        main.install_excepthook()
        assert sys.excepthook is not sys.__excepthook__

    def test_it_shows_the_problem_once(self, qapp, restore_excepthook, monkeypatch):
        shown = []
        monkeypatch.setattr(main.QMessageBox, "warning",
                            lambda *args, **kwargs: shown.append(args[2]))
        main.install_excepthook()

        try:
            raise ValueError("the bank is on fire")
        except ValueError:
            sys.excepthook(*sys.exc_info())

        assert len(shown) == 1
        assert "ValueError" in shown[0] and "the bank is on fire" in shown[0]
        assert "still running" in shown[0]

    def test_the_same_problem_twice_shows_one_dialog(
            self, qapp, restore_excepthook, monkeypatch):
        # A slot raising on every repaint would otherwise bury the screen.
        shown = []
        monkeypatch.setattr(main.QMessageBox, "warning",
                            lambda *args, **kwargs: shown.append(args[2]))
        main.install_excepthook()

        for _ in range(5):
            try:
                raise RuntimeError("again")
            except RuntimeError:
                sys.excepthook(*sys.exc_info())

        assert len(shown) == 1

    def test_a_different_problem_gets_its_own_dialog(
            self, qapp, restore_excepthook, monkeypatch):
        shown = []
        monkeypatch.setattr(main.QMessageBox, "warning",
                            lambda *args, **kwargs: shown.append(args[2]))
        main.install_excepthook()

        for error in (ValueError("one"), KeyError("two"), TypeError("three")):
            try:
                raise error
            except Exception:
                sys.excepthook(*sys.exc_info())

        assert len(shown) == 3

    def test_the_traceback_still_reaches_stderr(
            self, qapp, restore_excepthook, monkeypatch, capsys):
        monkeypatch.setattr(main.QMessageBox, "warning", lambda *a, **kw: None)
        main.install_excepthook()

        try:
            raise ValueError("written down as well")
        except ValueError:
            sys.excepthook(*sys.exc_info())

        assert "written down as well" in capsys.readouterr().err

    def test_a_failing_dialog_does_not_mask_the_error(
            self, qapp, restore_excepthook, monkeypatch, capsys):
        def refuse(*args, **kwargs):
            raise RuntimeError("no GUI here")

        monkeypatch.setattr(main.QMessageBox, "warning", refuse)
        main.install_excepthook()

        try:
            raise ValueError("the original problem")
        except ValueError:
            sys.excepthook(*sys.exc_info())      # must not raise

        assert "the original problem" in capsys.readouterr().err

    def test_ctrl_c_is_left_to_python(self, qapp, restore_excepthook, monkeypatch):
        shown = []
        monkeypatch.setattr(main.QMessageBox, "warning",
                            lambda *args, **kwargs: shown.append(args[2]))
        main.install_excepthook()

        try:
            raise KeyboardInterrupt
        except KeyboardInterrupt:
            sys.excepthook(*sys.exc_info())

        assert shown == [], "interrupting the app is not an error to report"
