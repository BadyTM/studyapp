"""Pre-renders every page blurred, once, at import time.

The quiz keeps the source visible at all times but unreadable until the answer is
in. Blurring live is the obvious approach and the wrong one: a `QGraphicsBlurEffect`
over a scrolling `QPdfView` flickers and repaints expensively.

Rendering the blurred pages up front costs a few megabytes and buys something
better than speed -- at runtime the app never opens the PDF with PyMuPDF at all.
Otherwise two PDF engines would be resident in one process, PyMuPDF to blur and
QtPdf to display, which is a pointless amount of memory and one more large
dependency in the eventual build.

Runs off the GUI thread. Writes files, never the database.
"""

import io
import os
from pathlib import Path

import pymupdf
from PIL import Image, ImageFilter

from .. import config

# Rendered smaller than it is displayed, which sounds wrong and is not: the blur
# destroys exactly the detail the extra resolution would have carried. Measured
# against a 1.5x PNG render, the displayed result differs by 0.6% per pixel --
# while taking a tenth of the time and an eighth of the space. For 200 pages that
# is the difference between 80 seconds and 32 MB, and 8 seconds and 4 MB.
ZOOM = 0.8

# Scaled to match: radius 3.5 at this zoom is the same blur as radius 6 at 1.5.
# Chosen against the mock-up -- individual words are gone, the shape of
# paragraphs and headings remains, so the pane still reads as a page of a book.
BLUR_RADIUS = 3.5

# JPEG, not PNG. There is no transparency to keep, and a smooth blur is the best
# case there is for JPEG; PNG with optimize=True was most of the 80 seconds.
FORMAT = "JPEG"
QUALITY = 80


def page_path(document_sha: str, page_number: int) -> Path:
    """Where one blurred page lives. Keyed by document hash, not by filename.

    An edited PDF hashes differently and so gets its own cache, which is what
    stops the previous edition's pages showing up behind the blur.
    """
    return config.blur_cache_dir(document_sha) / f"{page_number:04d}.jpg"


def is_complete(document_sha: str, page_count: int) -> bool:
    return all(page_path(document_sha, number).exists() for number in range(page_count))


def missing_pages(document_sha: str, page_count: int) -> list[int]:
    """Which pages still need rendering.

    Checked per page rather than per document so an interrupted render resumes
    instead of starting over.
    """
    return [number for number in range(page_count)
            if not page_path(document_sha, number).exists()]


def write_page(path: Path, data: bytes) -> None:
    """Writes one rendered page so that it is either absent or complete.

    `missing_pages` decides what is left to do by whether the file exists, which
    is what lets an interrupted render resume. A plain write breaks that promise:
    killed half way through it leaves a truncated JPEG that counts as finished
    for good, and the quiz shows a torn page with nothing offering to fix it.

    Renaming into place is atomic, so an interrupted render loses at most the
    page it was on -- which it would have re-rendered anyway.
    """
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def blur_pixmap(page: "pymupdf.Page", zoom: float = ZOOM,
                radius: float = BLUR_RADIUS) -> bytes:
    """One page, rasterised and blurred, as JPEG bytes."""
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
    image = image.filter(ImageFilter.GaussianBlur(radius))
    buffer = io.BytesIO()
    image.save(buffer, FORMAT, quality=QUALITY)
    return buffer.getvalue()


def render_document(
    pdf_path: str | Path,
    document_sha: str,
    zoom: float = ZOOM,
    radius: float = BLUR_RADIUS,
    on_progress=None,
    should_stop=None,
) -> int:
    """Renders whatever is missing. Returns how many pages were written.

    Safe to call again: pages already on disk are skipped, so a render stopped
    half way costs only the pages it had not reached.
    """
    written = 0
    with pymupdf.open(pdf_path) as document:
        pending = missing_pages(document_sha, document.page_count)
        for position, number in enumerate(pending, start=1):
            if should_stop and should_stop():
                break
            write_page(page_path(document_sha, number),
                       blur_pixmap(document[number], zoom, radius))
            written += 1
            if on_progress:
                on_progress(position, len(pending))
    return written


def cache_size_bytes(document_sha: str) -> int:
    directory = config.blur_cache_dir(document_sha)
    return sum(item.stat().st_size for item in directory.glob("*.jpg"))


def clear_cache(document_sha: str) -> int:
    """Deletes a document's blurred pages. Returns how many files went.

    Called when the document itself is removed: the cache is keyed by hash, so
    leaving it behind would be megabytes belonging to nothing.
    """
    directory = config.blur_cache_dir(document_sha)
    removed = 0
    for item in directory.glob("*.jpg"):
        item.unlink()
        removed += 1
    # A render killed mid-page leaves one of these behind. Not counted -- it was
    # never a page -- but taken with the rest, or the folder cannot go.
    for leftover in directory.glob("*.jpg.part"):
        leftover.unlink()
    try:
        directory.rmdir()
    except OSError:
        pass            # something else is in there; leaving it is harmless
    return removed
