"""The wiring between the tabs.

Each view is tested on its own elsewhere. What is not tested anywhere else is
the window that connects them, and the connections are the part that breaks
silently: a signal with no receiver raises nothing, emits nothing and looks
exactly like a signal that worked. Every one of them exists because something
went stale on another tab, so losing one brings that bug back -- the Generate
tab still listing a document the bank had removed, the mistakes queue still
offering questions a reset had cleared.

The window is built against an empty bank. Every slot runs for real, which is
the point: a wire that fires into a slot that cannot survive being called is
not a working wire.
"""

import pytest

from studyapp.db import connection as db_connection
from studyapp.ui.bank_view import BankView
from studyapp.ui.generate_view import GenerateView
from studyapp.ui.main_window import MainWindow
from studyapp.ui.quiz_view import QuizView, StartScreen
from studyapp.ui.stats_view import StatsView


@pytest.fixture
def window(qapp, isolated_data_dir):
    """A real main window over an empty bank in the isolated data directory."""
    db_connection.ensure_schema()
    window = MainWindow()
    yield window
    window.controller.shutdown(5_000)
    window.connection.close()


def spy(monkeypatch, owner, name, *, call_through=True):
    """Records calls to one method, before any window has bound it.

    Patched before construction on purpose: a connection binds the method as it
    is at connect time, so patching afterwards would record nothing and prove
    nothing. The original still runs -- several of these are called during
    construction, and a recorder that swallowed them would be testing a window
    that never finished being built.
    """
    heard = []
    original = getattr(owner, name)

    def recorder(self, *args, **kwargs):
        heard.append(args)
        if call_through:
            return original(self, *args, **kwargs)
        return None

    monkeypatch.setattr(owner, name, recorder)
    return heard


# Sender, its signal, what it carries, and the slot that has to hear it. Read
# as a sentence: "the bank changing reaches the start screen's refresh".
WIRES = [
    ("bank_view", "changed", (), StartScreen, "refresh"),
    ("bank_view", "changed", (), GenerateView, "reload_documents"),
    ("bank_view", "changed", (), StatsView, "refresh"),
    ("bank_view", "changed", (), QuizView, "bank_changed"),
    ("bank_view", "document_relocated", (1,), GenerateView, "ensure_blur"),
    ("bank_view", "document_relocated", (1,), QuizView, "document_moved"),
    ("generate_view", "changed", (), StatsView, "refresh"),
    ("generate_view", "changed", (), StartScreen, "refresh"),
    ("generate_view", "changed", (), QuizView, "document_moved"),
    ("generate_view", "changed", (), BankView, "reload_chapters"),
    ("generate_view", "changed", (), BankView, "reload_documents"),
    ("stats_view", "changed", (), StartScreen, "refresh"),
    ("stats_view", "changed", (), BankView, "refresh"),
    ("controller", "blur_ready", ("sha-rendered",), QuizView, "preview_ready"),
]


class TestEveryTabHearsTheOthers:

    @pytest.mark.parametrize("sender,signal,payload,owner,slot", WIRES,
                             ids=[f"{s}.{sig}->{o.__name__}.{m}"
                                  for s, sig, _, o, m in WIRES])
    def test_the_signal_reaches_the_slot(self, qapp, isolated_data_dir,
                                         monkeypatch, sender, signal, payload,
                                         owner, slot):
        heard = spy(monkeypatch, owner, slot)
        db_connection.ensure_schema()
        window = MainWindow()
        try:
            before = len(heard)
            getattr(getattr(window, sender), signal).emit(*payload)
            assert len(heard) > before, (
                f"{sender}.{signal} never reached {owner.__name__}.{slot}")
        finally:
            window.controller.shutdown(5_000)
            window.connection.close()


class TestTheWindowSOwnSlots:
    """The two receivers that are not another tab."""

    def test_the_controller_writes_to_the_status_bar(self, window):
        window.controller.status.emit("halfway through chunk 4")
        assert window.statusBar().currentMessage() == "halfway through chunk 4"

    def test_a_failure_is_put_in_front_of_the_reader(self, qapp,
                                                     isolated_data_dir,
                                                     monkeypatch):
        # Not called through: the real slot opens a modal dialog, and offscreen
        # or not, a test that waits for someone to click OK never ends.
        heard = spy(monkeypatch, MainWindow, "_show_failure", call_through=False)
        db_connection.ensure_schema()
        window = MainWindow()
        try:
            window.controller.failed.emit("the API refused the key")
            assert heard == [("the API refused the key",)],                 "a run that could not start would have failed in silence"
        finally:
            window.controller.shutdown(5_000)
            window.connection.close()

    def test_arriving_on_a_tab_refreshes_it(self, qapp, isolated_data_dir,
                                            monkeypatch):
        """Each tab re-reads the bank on the way in rather than being kept in
        step while hidden, so the wire that notices the tab changed is load
        bearing: without it a tab shows whatever it last saw."""
        heard = spy(monkeypatch, MainWindow, "_on_tab_changed")
        db_connection.ensure_schema()
        window = MainWindow()
        try:
            window.tabs.setCurrentIndex(1)
            assert heard, "switching tabs told the window nothing"
        finally:
            window.controller.shutdown(5_000)
            window.connection.close()


class TestAFinishedRenderReachesTheQuiz:
    """Kept apart from the table above: this one is about the signal being
    connected at all, which the table cannot see -- a wire removed while the
    slot stays behind leaves the slot passing its own tests, unreachable."""

    def test_the_signal_is_connected_at_all(self, window):
        assert window.controller.receivers("2blur_ready(QString)") >= 1
