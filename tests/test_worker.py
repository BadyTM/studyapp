"""Running generation across threads.

The test that matters most here is `test_only_one_thread_ever_writes`. The rule
it checks -- workers compute, one thread writes -- is the kind that holds until
someone adds a convenient `repo.insert_questions()` inside a worker two months
from now, at which point the failure is an intermittent `database is locked` that
looks like bad luck. Pinning it down with a test is cheaper than diagnosing that.
"""

import json
import threading
import time
from pathlib import Path

import pymupdf
import pytest
from PySide6.QtCore import QEventLoop, QTimer

from studyapp import config
from studyapp.db import connection as db_connection
from studyapp.db import repo
from studyapp.generate import worker as worker_module
from studyapp.generate.pipeline import ChunkResult
from studyapp.generate.worker import GenerationController

FIXTURES = Path(__file__).parent / "fixtures"
WRITING_SQL = ("insert", "update", "delete", "replace")


def reply(name: str) -> list[dict]:
    payload = json.loads((FIXTURES / "replies" / f"{name}.json").read_text(encoding="utf-8"))
    return payload["questions"]


class FakeBackend:
    """Answers instantly, and remembers which threads it was called from."""

    name = "fake"

    def __init__(self, questions=None, error=None, gate=None):
        self.questions = questions if questions is not None else []
        self.error = error
        self.threads: set[int] = set()
        self.calls = 0
        self.lock = threading.Lock()
        # Lets a test hold the backend open so it can act mid-run.
        self.gate = gate
        self.started = threading.Event()
        self.costs = type("Costs", (), {"status_text": lambda self: "free"})()

    def generate(self, system, user, is_cancelled=None):
        with self.lock:
            self.threads.add(threading.get_ident())
            self.calls += 1
        self.started.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.error:
            raise self.error
        return list(self.questions)


class WatchedConnection:
    """A connection that records the thread behind every statement it runs."""

    def __init__(self, connection):
        self._connection = connection
        self.writer_threads: set[int] = set()
        self.lock = threading.Lock()

    def execute(self, sql, *args, **kwargs):
        self._note(sql)
        return self._connection.execute(sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        self._note(sql)
        return self._connection.executemany(sql, *args, **kwargs)

    def _note(self, sql: str) -> None:
        if sql.strip().split(" ", 1)[0].lower() in WRITING_SQL:
            with self.lock:
                self.writer_threads.add(threading.get_ident())

    def __getattr__(self, name):
        return getattr(self._connection, name)


@pytest.fixture
def pdf(tmp_path) -> str:
    text = (FIXTURES / "chunk_transakce.txt").read_text(encoding="utf-8")
    path = tmp_path / "book.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((72, 300), "Titul", fontsize=20, fontname="hebo")
    for part in (text[:len(text) // 2], text[len(text) // 2:]):
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(60, 60, 400, 760), part,
                            fontsize=10, fontname="helv")
    document.save(path)
    document.close()
    return str(path)


class FakeChunk:
    def __init__(self, ordinal, text, title="6.2 Vlastnosti transakcí"):
        self.ordinal = ordinal
        self.page_start, self.page_end = 1, 2
        self.title = title
        self.text = text
        self.sha256 = f"sha-{ordinal}"


@pytest.fixture
def database(tmp_path, pdf):
    path = tmp_path / "test.db"
    db_connection.ensure_schema(path)
    connection = db_connection.connect(path)
    text = (FIXTURES / "chunk_transakce.txt").read_text(encoding="utf-8")
    document_id = repo.insert_document(connection, pdf, "book.pdf", "sha-book", 3, "cs")
    repo.insert_chunks(connection, document_id,
                       [FakeChunk(n, text) for n in range(6)])
    yield connection
    connection.close()


def doc_id(connection) -> int:
    """The id of the fixture document. sqlite3.Connection takes no attributes."""
    return int(repo.list_documents(connection)[0]["id"])


def run_to_completion(controller, start, timeout_ms=30_000) -> bool:
    """Starts a run and spins the event loop until it finishes."""
    loop = QEventLoop()
    controller.finished.connect(loop.quit)
    controller.failed.connect(lambda _: loop.quit())
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(timeout_ms)
    start()
    loop.exec()
    return controller.wait(5_000)


class TestSingleWriter:
    def test_only_one_thread_ever_writes(self, qapp, database, monkeypatch):
        # Four workers running at once, each producing questions to insert. Every
        # one of those inserts has to happen on the thread that owns the
        # connection; anything else is `database is locked` waiting to happen.
        backend = FakeBackend(reply("good"))
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)

        watched = WatchedConnection(database)
        controller = GenerationController(watched)
        run_to_completion(controller, lambda: controller.start(doc_id(database)))

        assert len(watched.writer_threads) == 1
        assert watched.writer_threads == {threading.get_ident()}

    def test_the_work_really_did_run_on_other_threads(self, qapp, database, monkeypatch):
        # Otherwise the test above would pass on a program that never threaded.
        backend = FakeBackend(reply("good"))
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)

        controller = GenerationController(database)
        run_to_completion(controller, lambda: controller.start(doc_id(database)))

        assert backend.calls == 6
        assert backend.threads and threading.get_ident() not in backend.threads


class TestRunAccounting:
    def test_every_chunk_is_written_and_marked_done(self, qapp, database, monkeypatch):
        backend = FakeBackend(reply("good"))
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)

        controller = GenerationController(database)
        run_to_completion(controller, lambda: controller.start(doc_id(database)))

        assert repo.chunk_status_counts(database, doc_id(database)) == {"done": 6}
        assert repo.count_questions(database) == 18
        assert repo.get_pending_chunks(database, doc_id(database)) == []

    def test_progress_counts_up_to_the_total(self, qapp, database, monkeypatch):
        monkeypatch.setattr(worker_module.client, "create_backend",
                            lambda name: FakeBackend(reply("good")))
        controller = GenerationController(database)
        seen = []
        controller.progress.connect(lambda done, total: seen.append((done, total)))
        run_to_completion(controller, lambda: controller.start(doc_id(database)))

        assert seen[0] == (0, 6)
        assert seen[-1] == (6, 6)
        assert [done for done, _ in seen] == sorted(done for done, _ in seen)

    def test_an_empty_reply_is_skipped_not_failed(self, qapp, database, monkeypatch):
        # A contents page has nothing to ask about, and counting that as a
        # failure hides the chunks that really did break.
        monkeypatch.setattr(worker_module.client, "create_backend",
                            lambda name: FakeBackend([]))
        controller = GenerationController(database)
        run_to_completion(controller, lambda: controller.start(doc_id(database)))

        assert repo.chunk_status_counts(database, doc_id(database)) == {"skipped": 6}

    def test_a_backend_failure_marks_the_chunk_and_keeps_the_reason(
            self, qapp, database, monkeypatch):
        monkeypatch.setattr(worker_module.client, "create_backend",
                            lambda name: FakeBackend(error=RuntimeError("HTTP 529")))
        controller = GenerationController(database)
        run_to_completion(controller, lambda: controller.start(doc_id(database)))

        assert repo.chunk_status_counts(database, doc_id(database)) == {"failed": 6}
        rows = repo.list_chunks(database, doc_id(database))
        assert all("529" in row["error"] for row in rows)

    def test_a_failed_run_leaves_the_chunks_ready_to_retry(
            self, qapp, database, monkeypatch):
        monkeypatch.setattr(worker_module.client, "create_backend",
                            lambda name: FakeBackend(error=RuntimeError("boom")))
        controller = GenerationController(database)
        run_to_completion(controller, lambda: controller.start(doc_id(database)))

        rows = repo.list_chunks(database, doc_id(database))
        controller.requeue([row["id"] for row in rows])
        assert len(repo.get_pending_chunks(database, doc_id(database))) == 6


class TestResuming:
    def test_chunks_left_running_are_picked_up_again(self, qapp, database):
        # This is what makes a run survive the app being closed half way: a
        # chunk marked `running` was never written, so it is still owed.
        rows = repo.list_chunks(database, doc_id(database))
        repo.set_chunk_status(database, rows[0]["id"], "running")
        repo.set_chunk_status(database, rows[1]["id"], "done")

        pending = repo.get_pending_chunks(database, doc_id(database))
        assert len(pending) == 5
        assert rows[0]["id"] in {row["id"] for row in pending}

    def test_a_second_run_only_takes_what_is_left(self, qapp, database, monkeypatch):
        backend = FakeBackend(reply("good"))
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)
        rows = repo.list_chunks(database, doc_id(database))
        for row in rows[:4]:
            repo.set_chunk_status(database, row["id"], "done")

        controller = GenerationController(database)
        run_to_completion(controller, lambda: controller.start(doc_id(database)))
        assert backend.calls == 2

    def test_regeneration_spares_hand_edited_questions(self, qapp, database, monkeypatch):
        backend = FakeBackend(reply("good"))
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)
        controller = GenerationController(database)
        run_to_completion(controller, lambda: controller.start(doc_id(database)))

        edited = database.execute("SELECT id FROM questions LIMIT 1").fetchone()["id"]
        database.execute("UPDATE questions SET edited_at = ?, question = ? WHERE id = ?",
                         (repo.utcnow(), "ručně opravená otázka", edited))
        database.commit()

        rows = repo.list_chunks(database, doc_id(database))
        controller.requeue([row["id"] for row in rows])
        run_to_completion(controller, lambda: controller.start(doc_id(database)))

        kept = database.execute("SELECT question, is_active FROM questions WHERE id = ?",
                                (edited,)).fetchone()
        assert kept["question"] == "ručně opravená otázka"
        assert kept["is_active"] == 1


class TestStarting:
    def test_nothing_pending_finishes_immediately(self, qapp, database, monkeypatch):
        monkeypatch.setattr(worker_module.client, "create_backend",
                            lambda name: FakeBackend(reply("good")))
        rows = repo.list_chunks(database, doc_id(database))
        for row in rows:
            repo.set_chunk_status(database, row["id"], "done")

        controller = GenerationController(database)
        finished = []
        controller.finished.connect(lambda: finished.append(True))
        controller.start(doc_id(database))
        assert finished == [True]
        assert not controller.is_running

    def test_no_backend_reports_rather_than_raises(self, qapp, database, monkeypatch):
        def refuse(name):
            raise worker_module.client.GenerationError("No way to reach Claude.")

        monkeypatch.setattr(worker_module.client, "create_backend", refuse)
        controller = GenerationController(database)
        messages = []
        controller.failed.connect(messages.append)
        controller.start(doc_id(database))

        assert messages == ["No way to reach Claude."]
        assert not controller.is_running

    def test_starting_twice_does_not_double_the_run(self, qapp, database, monkeypatch):
        backend = FakeBackend(reply("good"))
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)
        controller = GenerationController(database)

        def start_twice():
            controller.start(doc_id(database))
            controller.start(doc_id(database))

        run_to_completion(controller, start_twice)
        assert backend.calls == 6


class TestRunningJustSomeChunks:
    """`Regenerate selected` has to mean selected.

    Start runs everything pending, which is right for Start. Passing the ids
    keeps the other button honest: it named three rows, so three rows run.
    """

    def test_only_the_named_chunks_run(self, qapp, database, monkeypatch):
        backend = FakeBackend(reply("good"))
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)
        rows = repo.list_chunks(database, doc_id(database))
        chosen = [rows[1]["id"], rows[3]["id"]]

        controller = GenerationController(database)
        run_to_completion(
            controller,
            lambda: controller.start(doc_id(database), chunk_ids=chosen))

        assert backend.calls == 2, "the other four should have been left alone"
        counts = repo.chunk_status_counts(database, doc_id(database))
        assert counts == {"done": 2, "pending": 4}

    def test_without_ids_everything_pending_runs(self, qapp, database, monkeypatch):
        backend = FakeBackend(reply("good"))
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)
        controller = GenerationController(database)
        run_to_completion(controller, lambda: controller.start(doc_id(database)))
        assert backend.calls == 6

    def test_naming_a_chunk_that_is_not_pending_runs_nothing(
            self, qapp, database, monkeypatch):
        backend = FakeBackend(reply("good"))
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)
        rows = repo.list_chunks(database, doc_id(database))
        repo.set_chunk_status(database, rows[0]["id"], "done")

        controller = GenerationController(database)
        finished = []
        controller.finished.connect(lambda: finished.append(True))
        controller.start(doc_id(database), chunk_ids=[rows[0]["id"]])

        assert backend.calls == 0
        assert finished == [True], "it must still end, not hang"
        assert not controller.is_running


class TestStopping:
    def test_stop_leaves_queued_chunks_untouched(self, qapp, database, monkeypatch):
        # The backend is held open until stop() has been called, so the jobs
        # still queued behind it are guaranteed to see the cancel flag. Without
        # the gate this raced: an instant backend can finish all six before stop
        # lands, and the test then passes or fails on timing alone.
        gate = threading.Event()
        backend = FakeBackend(reply("good"), gate=gate)
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)

        controller = GenerationController(database)
        controller.start(doc_id(database))
        backend.started.wait(5)         # the pool is busy
        controller.stop()
        gate.set()
        controller.wait(10_000)

        # Cancelled jobs return without emitting, so those chunks stay `running`
        # -- which the next start treats as still owed.
        assert backend.calls <= config.MAX_PARALLEL_CHUNKS
        assert backend.calls < 6

    def test_the_run_ends_after_a_stop(self, qapp, database, monkeypatch):
        # It did not. Cancelled jobs returned without emitting, so the count of
        # finished chunks never reached the number queued: `finished` never
        # fired, `is_running` stayed True, and Start was dead for the rest of
        # the session.
        gate = threading.Event()
        backend = FakeBackend(reply("good"), gate=gate)
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)

        controller = GenerationController(database)
        controller.start(doc_id(database))
        backend.started.wait(5)
        controller.stop()
        gate.set()
        controller.wait(10_000)
        qapp.processEvents()

        assert not controller.is_running

    def test_a_stopped_run_can_be_started_again(self, qapp, database, monkeypatch):
        gate = threading.Event()
        backend = FakeBackend(reply("good"), gate=gate)
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)

        controller = GenerationController(database)
        controller.start(doc_id(database))
        backend.started.wait(5)
        controller.stop()
        gate.set()
        controller.wait(10_000)
        qapp.processEvents()

        before = backend.calls
        run_to_completion(controller, lambda: controller.start(doc_id(database)))
        assert backend.calls > before, "the second run did nothing"

    def test_cancelled_chunks_go_back_to_pending(self, qapp, database, monkeypatch):
        # They were marked `running` before their job started. Left that way the
        # table shows a run in progress that nothing is working on.
        gate = threading.Event()
        backend = FakeBackend(reply("good"), gate=gate)
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)

        controller = GenerationController(database)
        controller.start(doc_id(database))
        backend.started.wait(5)
        controller.stop()
        gate.set()
        controller.wait(10_000)
        qapp.processEvents()

        assert "running" not in repo.chunk_status_counts(database, doc_id(database))

    def test_every_chunk_is_accounted_for_after_a_stop(self, qapp, database, monkeypatch):
        gate = threading.Event()
        backend = FakeBackend(reply("good"), gate=gate)
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)

        controller = GenerationController(database)
        seen = []
        controller.progress.connect(lambda done, total: seen.append((done, total)))
        controller.start(doc_id(database))
        backend.started.wait(5)
        controller.stop()
        gate.set()
        controller.wait(10_000)
        qapp.processEvents()

        assert seen[-1] == (6, 6), "the progress should reach the total either way"

    def test_shutdown_stops_the_preview_render_too(self, qapp, database):
        # Separate flags: stopping a generation run must not abort a blur
        # render, but closing the window has to stop both or it waits on one.
        controller = GenerationController(database)
        assert controller.cancel is not controller.blur_cancel
        controller.shutdown(1_000)
        assert controller.cancel.is_set() and controller.blur_cancel.is_set()

    def test_stop_before_a_run_does_nothing(self, qapp, database):
        controller = GenerationController(database)
        controller.stop()
        assert not controller.is_running

    def test_shutdown_banks_a_result_that_is_still_in_the_queue(
            self, qapp, database, monkeypatch):
        # The window closes a moment after a chunk came back. `waitForDone`
        # returns true -- the job really has finished -- but its result crossed
        # threads as a queued signal and has not been delivered yet. Closing the
        # connection there loses questions that were generated and paid for.
        backend = FakeBackend(questions=reply("good"))
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)

        controller = GenerationController(database)
        controller.start(doc_id(database), limit=1)
        assert backend.started.wait(10), "the job never ran"
        # Deliberately no processEvents: the result sits in the queue, exactly
        # as it would when someone closes the window at the wrong moment.
        controller.wait(10_000)

        controller.shutdown(5_000)

        assert repo.count_questions(database) > 0,             "the queued result was dropped instead of written"
        assert "running" not in repo.chunk_status_counts(database, doc_id(database)),             "a chunk was left mid-flight with nothing coming to finish it"

    def test_nothing_is_written_after_shutdown(self, qapp, database, monkeypatch):
        # The other half: a job that outlives the wait must not write, because
        # the window has closed the connection by then.
        backend = FakeBackend(questions=reply("good"))
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)

        controller = GenerationController(database)
        controller.shutdown(100)
        before = repo.count_questions(database)
        controller._on_chunk_done(ChunkResult(chunk_id=1, questions=[{"x": 1}]))

        assert repo.count_questions(database) == before


class TestImporting:
    def test_a_pdf_is_read_off_the_gui_thread_and_written_on_it(
            self, qapp, tmp_path, pdf):
        path = tmp_path / "fresh.db"
        db_connection.ensure_schema(path)
        connection = db_connection.connect(path)
        watched = WatchedConnection(connection)
        controller = GenerationController(watched)

        loop = QEventLoop()
        results = []
        controller.imported.connect(lambda document_id, is_new: (
            results.append((document_id, is_new)), loop.quit()))
        controller.failed.connect(lambda _: loop.quit())
        QTimer.singleShot(20_000, loop.quit)
        controller.import_pdf(pdf)
        loop.exec()

        assert results and results[0][1] is True
        assert watched.writer_threads == {threading.get_ident()}
        assert repo.list_documents(connection)
        connection.close()

    def test_a_bad_file_is_reported_not_raised(self, qapp, database, tmp_path):
        controller = GenerationController(database)
        loop = QEventLoop()
        messages = []
        controller.failed.connect(lambda message: (messages.append(message), loop.quit()))
        QTimer.singleShot(10_000, loop.quit)
        controller.import_pdf(str(tmp_path / "does-not-exist.pdf"))
        loop.exec()

        assert messages and "No such file" in messages[0]


class TestReimportingAMovedFile:
    def test_importing_the_same_book_from_a_new_path_corrects_the_old_one(
            self, qapp, tmp_path, pdf):
        """Re-importing is what someone tries first after moving a file.

        It used to find the document by hash, report "already imported", and
        leave the stale path in place -- so the obvious fix did nothing and said
        nothing.
        """
        from studyapp.ingest import importer

        path = tmp_path / "reimport.db"
        db_connection.ensure_schema(path)
        connection = db_connection.connect(path)
        try:
            first = importer.store(connection, importer.prepare(pdf, 50, 200))
            document_id, is_new = first
            assert is_new

            moved = Path(tmp_path) / "renamed book.pdf"
            Path(pdf).rename(moved)

            again_id, again_new = importer.store(
                connection, importer.prepare(moved, 50, 200))

            assert again_id == document_id, "it is the same book"
            assert not again_new, "and not a second copy of it"
            row = repo.get_document(connection, document_id)
            assert Path(row["path"]).name == "renamed book.pdf"
            assert row["filename"] == "renamed book.pdf"
        finally:
            connection.close()


class TestAFinishedJobsLastSignalsStillArrive:
    """The GUI thread is busy at the moment a job ends -- which is the normal case.

    A finished QRunnable is deleted by the pool, and in PySide that takes the
    signal object it borrowed with it. Signals already emitted but not yet
    picked up by the GUI thread go with it, so the ones a job emits last are the
    ones that vanish: the end of a blur render, and `blur_ready` itself.
    """

    def test_blur_ready_survives_a_busy_gui_thread(self, qapp, database, pdf):
        controller = GenerationController(database)
        ready, said = [], []
        controller.blur_ready.connect(ready.append)
        controller.status.connect(said.append)

        controller.prerender_blur(pdf, "sha-busy-thread", 3)
        # Not pumping is the point: the render finishes and is deleted while the
        # GUI thread is elsewhere. Everything it emitted has to be waiting.
        controller.wait(30_000)
        time.sleep(0.2)
        qapp.processEvents()

        assert ready == ["sha-busy-thread"], "the render finished in silence"
        assert any("page 3 of 3" in message for message in said), said

    def test_a_job_is_let_go_of_once_it_has_been_heard(self, qapp, database, pdf):
        """Held forever would be a leak: one chunk's text per chunk of a book."""
        controller = GenerationController(database)
        done = []
        controller.blur_ready.connect(done.append)

        controller.prerender_blur(pdf, "sha-let-go", 3)
        assert controller._jobs, "the job was not held in the first place"

        deadline = time.monotonic() + 30
        while not done and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        for _ in range(20):            # the release is deferred by one tick
            qapp.processEvents()
            time.sleep(0.01)

        assert done, "the render never reported finishing"
        assert not controller._jobs, "the finished job was never let go of"


class TestTheStatusLineIsTidiedUp:
    """Only `progress` wrote to the status bar during a blur render.

    So the last page it reported stayed there for the rest of the session:
    "Preparing the preview: page 116 of 116" sitting under a quiz that had been
    running for ten minutes. Seen in a screenshot of the real app, not in a test.

    Each test renders under its own hash: the fixture PDF is identical every
    time, so sharing one would mean the second test finds a finished cache and
    renders nothing.
    """

    def rendered(self, qapp, database, pdf, sha):
        """Renders and pumps the loop while it runs, as the window does.

        Waiting for the pool and pumping afterwards loses the messages: a
        finished QRunnable is deleted along with the signal object it borrowed,
        and anything still queued on it goes with it. The real window never has
        this problem because its event loop is running the whole time.
        """
        controller = GenerationController(database)
        said = []
        controller.status.connect(said.append)
        finished = []
        controller.blur_ready.connect(finished.append)

        controller.prerender_blur(pdf, sha, 3)
        deadline = time.monotonic() + 30
        while not finished and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        qapp.processEvents()
        return controller, said

    def test_a_finished_render_takes_its_message_down(self, qapp, database, pdf):
        _, said = self.rendered(qapp, database, pdf, "sha-status-down")
        assert said, "the render said nothing at all"
        assert said[-1] == "", f"the status bar was left saying {said[-1]!r}"

    def test_the_progress_line_still_appears_while_it_runs(self, qapp, database, pdf):
        _, said = self.rendered(qapp, database, pdf, "sha-status-progress")
        assert any("Preparing the preview" in message for message in said), said

    def test_a_render_during_a_run_does_not_blank_the_run_s_status(
            self, qapp, database, pdf, monkeypatch):
        # The run writes the cost line and the chunk count there; a background
        # render finishing must not wipe them.
        backend = FakeBackend(questions=reply("good"))
        monkeypatch.setattr(worker_module.client, "create_backend", lambda name: backend)
        controller = GenerationController(database)
        controller.start(doc_id(database), limit=1)

        said = []
        controller.status.connect(said.append)
        controller._on_blur_done("sha-during-run")

        assert "" not in said, "a finishing render blanked a running generation"
        controller.shutdown(10_000)

    def test_an_already_rendered_document_says_nothing_new(self, qapp, database, pdf):
        from studyapp.ingest import blur

        sha = "sha-already-done"
        blur.render_document(pdf, sha)

        controller = GenerationController(database)
        said = []
        controller.status.connect(said.append)
        ready = []
        controller.blur_ready.connect(ready.append)

        controller.prerender_blur(pdf, sha, 3)
        qapp.processEvents()

        assert ready == [sha], "a complete cache should report ready at once"
        assert not said, f"nothing to do, but it said {said}"
