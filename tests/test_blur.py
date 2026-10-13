"""The pre-rendered blurred pages.

The one property worth asserting rather than eyeballing: the output is actually
unreadable. A blur radius accidentally set to zero would still produce files, the
app would still run, and the preview would quietly be showing the answer.
"""

import io
from pathlib import Path

import numpy as np
import pymupdf
import pytest
from PIL import Image

from studyapp.ingest import blur


@pytest.fixture(scope="module")
def pdf(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("blur") / "book.pdf"
    document = pymupdf.open()
    for number in range(4):
        page = document.new_page()
        page.insert_textbox(
            pymupdf.Rect(60, 60, 500, 700),
            f"Kapitola {number}\n\n" + "Transakce je posloupnost operaci. " * 60,
            fontsize=11, fontname="helv")
    document.save(path)
    document.close()
    return str(path)


def sharpness(image: Image.Image) -> float:
    """Mean absolute difference between neighbouring pixels.

    High where edges are crisp -- which, on a page of text, means readable.
    """
    array = np.asarray(image.convert("L"), dtype=float)
    return float(np.abs(np.diff(array, axis=1)).mean())


class TestBlurring:
    def test_the_output_is_genuinely_blurred(self, pdf):
        with pymupdf.open(pdf) as document:
            page = document[0]
            sharp = Image.open(io.BytesIO(
                page.get_pixmap(matrix=pymupdf.Matrix(blur.ZOOM, blur.ZOOM))
                .tobytes("png")))
            blurred = Image.open(io.BytesIO(blur.blur_pixmap(page)))

        # An order of magnitude is not a close call; a radius of zero would put
        # these two within a few percent of each other.
        assert sharpness(blurred) < sharpness(sharp) / 5

    def test_it_produces_a_readable_image_file(self, pdf):
        with pymupdf.open(pdf) as document:
            data = blur.blur_pixmap(document[0])
        image = Image.open(io.BytesIO(data))
        assert image.format == "JPEG"
        assert image.size[0] > 100


class TestCache:
    def test_rendering_writes_one_file_per_page(self, pdf):
        sha = "test-cache-render"
        assert blur.render_document(pdf, sha) == 4
        assert blur.is_complete(sha, 4)
        assert blur.cache_size_bytes(sha) > 0

    def test_rendering_again_does_nothing(self, pdf):
        # Called on every import; re-rendering 200 pages each time would be a
        # minute of pointless work.
        sha = "test-cache-idempotent"
        blur.render_document(pdf, sha)
        assert blur.render_document(pdf, sha) == 0

    def test_an_interrupted_render_resumes(self, pdf):
        # Checked per page, so stopping half way costs only what it had not reached.
        sha = "test-cache-resume"
        stop_after = {"n": 0}

        def should_stop():
            stop_after["n"] += 1
            return stop_after["n"] > 2

        blur.render_document(pdf, sha, should_stop=should_stop)
        assert not blur.is_complete(sha, 4)
        remaining = len(blur.missing_pages(sha, 4))
        assert blur.render_document(pdf, sha) == remaining
        assert blur.is_complete(sha, 4)

    def test_a_page_killed_mid_write_is_not_counted_as_done(self, pdf, monkeypatch):
        # `missing_pages` asks only whether the file exists, so a page torn in
        # half by a render that died mid-write would be taken for a finished one
        # for good -- one permanently broken preview, and nothing that would
        # ever offer to render it again.
        sha = "test-cache-torn"
        intact = Path.write_bytes

        def die_half_way(self, data):
            intact(self, data[:len(data) // 2])
            raise KeyboardInterrupt("killed mid-write")

        monkeypatch.setattr(Path, "write_bytes", die_half_way)
        with pytest.raises(KeyboardInterrupt):
            blur.render_document(pdf, sha)
        monkeypatch.undo()

        assert blur.missing_pages(sha, 4) == [0, 1, 2, 3],             "a half-written page must not count as rendered"

        blur.render_document(pdf, sha)
        assert blur.is_complete(sha, 4)
        # Every page opens, so none of them is the torn one.
        for number in range(4):
            Image.open(blur.page_path(sha, number)).verify()
        # And the half-written file is not left lying in the cache.
        assert not list(blur.config.blur_cache_dir(sha).glob("*.part"))

    def test_progress_is_reported(self, pdf):
        seen = []
        blur.render_document(pdf, "test-cache-progress",
                             on_progress=lambda done, total: seen.append((done, total)))
        assert seen[0] == (1, 4)
        assert seen[-1] == (4, 4)

    def test_a_different_document_gets_its_own_cache(self, pdf):
        # Keyed by hash: an edited PDF must not show the old edition's pages
        # behind the blur.
        blur.render_document(pdf, "test-cache-a")
        assert not blur.is_complete("test-cache-b", 4)
        assert blur.page_path("test-cache-a", 0) != blur.page_path("test-cache-b", 0)

    def test_an_unrendered_document_reports_incomplete(self):
        assert not blur.is_complete("test-cache-never-rendered", 10)
        assert blur.missing_pages("test-cache-never-rendered", 3) == [0, 1, 2]


@pytest.fixture(autouse=True, scope="module")
def _clean_caches():
    yield
    import shutil

    from studyapp import config

    for name in ("test-cache-render", "test-cache-idempotent", "test-cache-resume",
                 "test-cache-progress", "test-cache-a", "test-cache-b",
                 "test-cache-never-rendered"):
        shutil.rmtree(config.blur_cache_dir(name), ignore_errors=True)
