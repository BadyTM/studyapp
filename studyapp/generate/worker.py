"""Running generation across several chunks without freezing the window.

The rule this file exists to enforce: **workers compute, one thread writes.**
A `sqlite3` connection cannot be shared between threads, and three or four
workers each opening their own and inserting would produce `database is locked`
non-deterministically -- which is the worst way for a bug to arrive, because it
looks like bad luck rather than a design error.

So a `ChunkJob` does the network call, the validation and the rectangle search,
and then does nothing with the answer except emit it. `GenerationController`
lives in the GUI thread, receives those signals one at a time, and is the only
thing in the program that writes during a run.
"""

import threading

from PySide6.QtCore import (
    QCoreApplication,
    QEventLoop,
    QObject,
    QRunnable,
    QThreadPool,
    QTimer,
    Signal,
    Slot,
)

from .. import config
from ..db import repo
from ..ingest import blur, importer
from . import client
from .pipeline import ChunkResult, generate_for_chunk


class JobSignals(QObject):
    """A QRunnable cannot carry signals itself, so it borrows these."""

    done = Signal(object)               # ChunkResult
    prepared = Signal(object)           # importer.Prepared
    progress = Signal(int, int)         # completed, total
    failed = Signal(str)


class ChunkJob(QRunnable):
    """Generates one chunk on the thread pool. Never touches the database."""

    def __init__(self, chunk, document, backend, cancel: threading.Event):
        super().__init__()
        self.signals = JobSignals()
        # Rows are read out here, in the thread that owns the connection. A
        # sqlite3.Row read from a worker would be reaching across threads.
        self.chunk = {key: chunk[key] for key in
                      ("id", "text", "title", "page_start", "page_end")}
        self.document = {key: document[key] for key in ("id", "path", "language")}
        self.backend = backend
        self.cancel = cancel

    @Slot()
    def run(self) -> None:
        if self.cancel.is_set():
            # Reported, not dropped. The controller finishes a run by counting
            # results against the number of chunks it queued, so a job that
            # returns in silence leaves it waiting for a result that will never
            # come -- the run never ends and Start never comes back.
            self.signals.done.emit(
                ChunkResult(chunk_id=self.chunk["id"], cancelled=True))
            return
        result = generate_for_chunk(self.backend, self.chunk, self.document,
                                    is_cancelled=self.cancel.is_set)
        self.signals.done.emit(result)


class ImportJob(QRunnable):
    """Reads and chunks a PDF off the GUI thread. Writes nothing."""

    def __init__(self, path, min_words: int, max_words: int):
        super().__init__()
        self.signals = JobSignals()
        self.path = path
        self.min_words, self.max_words = min_words, max_words

    @Slot()
    def run(self) -> None:
        try:
            self.signals.prepared.emit(
                importer.prepare(self.path, self.min_words, self.max_words))
        except Exception as error:      # noqa: BLE001 - shown to the user
            self.signals.failed.emit(str(error))


class BlurJob(QRunnable):
    """Pre-renders the blurred pages of a document. Writes files, not rows."""

    def __init__(self, pdf_path, document_sha: str, cancel: threading.Event):
        super().__init__()
        self.signals = JobSignals()
        self.pdf_path = pdf_path
        self.document_sha = document_sha
        self.cancel = cancel

    @Slot()
    def run(self) -> None:
        try:
            blur.render_document(
                self.pdf_path, self.document_sha,
                on_progress=lambda done, total: self.signals.progress.emit(done, total),
                should_stop=self.cancel.is_set,
            )
        except Exception as error:      # noqa: BLE001 - shown, not swallowed
            self.signals.failed.emit(str(error))
        self.signals.done.emit(None)


class GenerationController(QObject):
    """Owns the run: the pool, the backend, the cancel flag and the writing.

    Every slot here runs in the GUI thread, which is what makes it the single
    writer. It never blocks: the only work it does is a few hundred INSERTs
    spread across the run.
    """

    progress = Signal(int, int)         # completed, total
    chunk_done = Signal(int, str, int)  # chunk_id, status, questions written
    status = Signal(str)                # a line for the status bar
    finished = Signal()
    failed = Signal(str)                # the run could not start at all
    imported = Signal(int, bool)        # document_id, is_new
    blur_ready = Signal(str)            # document_sha

    def __init__(self, connection, parent=None):
        super().__init__(parent)
        self.connection = connection
        self.pool = QThreadPool()
        self.backend = None
        self.cancel = threading.Event()
        # Its own flag, because the two are unrelated work. Sharing one meant
        # stopping a generation run also aborted a blur render half way, which
        # left the quiz with pages it could not show and nothing to retry it.
        self.blur_cancel = threading.Event()
        # Jobs that have been started and whose last signal has not been seen
        # yet. See `_own`: letting go too early loses the signal.
        self._jobs = set()
        self._total = 0
        self._completed = 0
        self._running = False
        # Set once the window is closing and the connection is about to go. A
        # job that outlived the wait still emits its result, and writing it then
        # would be writing to a closed database.
        self._closed = False

    # --- keeping jobs alive -------------------------------------------------

    def _own(self, job, *last) -> None:
        """Keeps a finished job alive until its last signals are delivered.

        QThreadPool deletes a QRunnable the moment `run` returns, and in PySide
        that takes the `JobSignals` it borrowed with it. A signal connected to a
        bound method survives it -- the queued event belongs to the receiver,
        and destroying the sender does not touch the receiver's queue -- but one
        connected to a lambda does not, because then the receiver is owned by
        the connection and dies with it.

        So whatever a job emits last is what gets thrown away. Measured on a
        three-page blur render: pages 1 and 2 were reported, then it went quiet,
        and `blur_ready` never arrived at all -- while the identical run with a
        reference held to the job reported all three and finished. In the app
        that leaves the preview permanently "not ready": nothing tells the quiz
        it can stop waiting, and the status line keeps the last page it managed
        to report. That is the "Preparing the preview: page 116 of 116" that sat
        under a running quiz.

        Holding the job is the fix rather than giving the signals a parent here.
        A parent in the GUI thread would be destroyed with this controller,
        which can happen while the job is still running and still emitting on
        them -- that is not a lost signal, it is an abort.
        """
        self._jobs.add(job)
        for signal in last:
            # Deferred rather than dropped here and now: this runs while Qt is
            # delivering that very signal, and letting go of the last reference
            # mid-delivery is the problem above in miniature.
            signal.connect(lambda *_, job=job: QTimer.singleShot(
                0, lambda: self._jobs.discard(job)))

    # --- importing ----------------------------------------------------------

    def import_pdf(self, path) -> None:
        """Imports a PDF in the background and writes the result here."""
        job = ImportJob(path, config.CHUNK_MIN_WORDS, config.CHUNK_MAX_WORDS)
        job.signals.prepared.connect(self._on_prepared)
        job.signals.failed.connect(self.failed)
        self._own(job, job.signals.prepared, job.signals.failed)
        self.status.emit(self.tr("Reading the PDF ..."))
        self.pool.start(job)

    @Slot(object)
    def _on_prepared(self, prepared) -> None:
        if self._closed:
            return
        document_id, is_new = importer.store(self.connection, prepared)
        self.imported.emit(document_id, is_new)
        self.status.emit(
            self.tr("Imported {0} chunks.").format(len(prepared.chunks)) if is_new
            else self.tr("Already imported.")
        )
        self.prerender_blur(prepared.path, prepared.sha256, prepared.page_count)

    def prerender_blur(self, pdf_path, document_sha: str, page_count: int) -> None:
        """Renders the blurred pages the quiz needs, in the background.

        Started after the import is stored rather than before, so the document
        shows up in the list straight away -- the render takes a while on 200
        pages and nothing else waits on it.
        """
        if blur.is_complete(document_sha, page_count):
            self.blur_ready.emit(document_sha)
            return
        job = BlurJob(pdf_path, document_sha, self.blur_cancel)
        job.signals.progress.connect(
            lambda done, total: self.status.emit(
                self.tr("Preparing the preview: page {0} of {1}").format(done, total)))
        job.signals.done.connect(lambda _: self._on_blur_done(document_sha))
        job.signals.failed.connect(self.failed)
        self._own(job, job.signals.done)
        self.pool.start(job)

    def _on_blur_done(self, document_sha: str) -> None:
        """Clears the progress line the render was writing, and says it is ready.

        Only `progress` ever wrote to the status bar, so the last page it
        reported stayed there for the rest of the session -- "Preparing the
        preview: page 116 of 116" sitting under a quiz that had been running for
        ten minutes. A finished job has to take its own message down.

        Left alone during a generation run: that writes its own status, and
        blanking it would hide the cost line and the chunk count.
        """
        if not self._running:
            self.status.emit("")
        self.blur_ready.emit(document_sha)

    # --- generating ---------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, document_id: int, backend_name: str | None = None,
              limit: int | None = None, chunk_ids=None) -> None:
        """Runs the pending chunks of a document, or just the ones named.

        `chunk_ids` is what makes "Regenerate selected" mean it: without it the
        run sweeps up everything pending, which is right for Start and wrong for
        a button that named three rows.
        """
        if self._running:
            return
        try:
            self.backend = client.create_backend(backend_name)
        except client.GenerationError as error:
            self.failed.emit(str(error))
            return

        chunks = repo.get_pending_chunks(self.connection, document_id, limit)
        if chunk_ids is not None:
            wanted = set(chunk_ids)
            chunks = [chunk for chunk in chunks if chunk["id"] in wanted]
        if not chunks:
            self.status.emit(self.tr("Nothing pending."))
            self.finished.emit()
            return

        document = repo.get_document(self.connection, document_id)
        self.cancel.clear()
        self._total, self._completed = len(chunks), 0
        self._running = True

        # Above four or five in flight the API starts refusing and the backoff
        # eats whatever the extra parallelism gained.
        self.pool.setMaxThreadCount(config.MAX_PARALLEL_CHUNKS)

        for chunk in chunks:
            # Marked before the job runs, so an app killed mid-run leaves the
            # chunk visibly unfinished rather than silently pending.
            repo.set_chunk_status(self.connection, chunk["id"], "running")
            job = ChunkJob(chunk, document, self.backend, self.cancel)
            job.signals.done.connect(self._on_chunk_done)
            self._own(job, job.signals.done)
            self.pool.start(job)

        self.progress.emit(0, self._total)
        self.status.emit(self.tr("Generating with {0} ...").format(self.backend.name))

    @Slot(object)
    def _on_chunk_done(self, result: ChunkResult) -> None:
        """The single writer. Runs in the GUI thread, one result at a time."""
        if self._closed:
            # The window has gone and taken the connection with it. The chunk
            # stays `running`, which `get_pending_chunks` already treats as work
            # to pick up again, so the next run collects it.
            return
        written = 0
        if result.cancelled:
            # Nothing was generated, so nothing is written beyond putting the
            # chunk back in the queue.
            repo.set_chunk_status(self.connection, result.chunk_id, "pending")
            self._finish_one(result, written)
            return

        if result.questions:
            # Anything hand-edited survives; only generated questions are retired.
            repo.deactivate_unedited_questions(self.connection, result.chunk_id)
            written = repo.insert_questions(self.connection, result.chunk_id,
                                            result.questions)
        repo.set_chunk_status(self.connection, result.chunk_id, result.status,
                              result.error)
        self._finish_one(result, written)

    def _finish_one(self, result: ChunkResult, written: int) -> None:
        """Counts one chunk off the run, and ends the run when none are left."""
        self._completed += 1
        self.chunk_done.emit(result.chunk_id, result.status, written)
        self.progress.emit(self._completed, self._total)
        if self.backend is not None and not result.cancelled:
            self.status.emit(self.backend.costs.status_text())

        if self._completed >= self._total:
            self._running = False
            self.finished.emit()

    def stop(self) -> None:
        """Asks the run to stop after the chunks already in flight.

        A request that is already open is left to finish. It has been paid for
        either way, and abandoning the reply would lose the questions without
        saving anything.
        """
        if not self._running:
            return
        self.cancel.set()
        self.status.emit(self.tr("Stopping after the chunks in flight ..."))

    def requeue(self, chunk_ids) -> None:
        """Marks chunks for regeneration."""
        repo.reset_chunks(self.connection, list(chunk_ids))
        self.status.emit(self.tr("{0} chunks queued again.").format(len(chunk_ids)))

    def shutdown(self, timeout_ms: int = 5_000) -> bool:
        """Stops everything, banks what has already come back, and waits.

        Both flags, not just the generation one: a blur render left running
        would hold the pool for as long as it had pages to go, and the window
        would sit there refusing to close.

        The drain in the middle is the part that is easy to leave out, and it
        cost real questions. `waitForDone` waits for the jobs; it says nothing
        about the results they have already handed over, because those cross
        threads as queued signals and are delivered by the event loop -- which by
        now is on its way out. Without the drain the last chunks of a run are
        generated, paid for, and then dropped on the floor as the connection
        closes underneath them.
        """
        self.cancel.set()
        self.blur_cancel.set()
        finished = self.pool.waitForDone(timeout_ms)
        QCoreApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
        # Anything still running missed the drain and must not write afterwards.
        self._closed = True
        return finished

    def wait(self, timeout_ms: int = 30_000) -> bool:
        """Blocks until the pool is idle. For tests, and for shutdown."""
        return self.pool.waitForDone(timeout_ms)
