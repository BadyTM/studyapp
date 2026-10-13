"""Importing a PDF, split at the point where the database gets involved.

`prepare` does the slow part -- reading 200 pages, chunking them -- and touches
nothing but the file. `store` does the writing and nothing else. The split is
what lets the window import without freezing: a worker thread calls `prepare`,
and the GUI thread, which is the only writer, calls `store`.
"""

from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..db import repo
from . import extract, language
from .chunker import Chunk, chunk_pages


class ImportError_(Exception):
    """Something about the file itself makes it unusable."""


@dataclass
class Prepared:
    """A PDF read and chunked, but not yet written anywhere."""

    path: Path
    sha256: str
    page_count: int
    language: str | None
    chunks: list[Chunk]

    @property
    def word_counts(self) -> list[int]:
        return [chunk.word_count for chunk in self.chunks]

    def summary(self) -> str:
        counts = self.word_counts
        titled = sum(1 for chunk in self.chunks if chunk.title)
        return (
            f"{self.page_count} pages -> {len(self.chunks)} chunks\n"
            f"words per chunk: min {min(counts)}, mean {sum(counts) // len(counts)}, "
            f"max {max(counts)}\n"
            f"chunks with a chapter title: {titled}/{len(self.chunks)}"
        )


def prepare(
    path: str | Path,
    min_words: int = config.CHUNK_MIN_WORDS,
    max_words: int = config.CHUNK_MAX_WORDS,
    source_language: str | None = None,
) -> Prepared:
    """Reads and chunks a PDF. No database, safe to run off the GUI thread."""
    path = Path(path)
    if not path.exists():
        raise ImportError_(f"No such file: {path}")

    pages = extract.extract_pages(path)

    # Asked before anything else: without a text layer there are no quotes, no
    # page numbers and no highlights, and every later phase rests on all three.
    if not extract.has_text_layer(pages):
        raise ImportError_(
            "This PDF has no text layer -- it is a scan. Run it through OCR first "
            "(ocrmypdf, or Acrobat's Recognise Text) and import the result."
        )

    chunks = chunk_pages(pages, min_words, max_words)
    if not chunks:
        raise ImportError_("The PDF has pages but no usable text.")

    detected = source_language or language.detect("\n".join(page.text for page in pages))
    return Prepared(
        path=path.resolve(),
        sha256=extract.file_sha256(path),
        page_count=len(pages),
        language=detected,
        chunks=chunks,
    )


def store(connection, prepared: Prepared, replace: bool = False) -> tuple[int, bool]:
    """Writes a prepared import. Returns the document id and whether it is new.

    Re-importing the same file is a no-op rather than an error: `documents.sha256`
    is unique, and a second copy of the same book would only split its question
    bank in two.

    `replace` re-chunks it instead -- for when the chunker itself has changed and
    the stored chunks are the ones that are wrong. It refuses once there are
    questions, because re-chunking discards them and everything answered against
    them; retire or regenerate those instead.
    """
    existing = repo.get_document_by_sha(connection, prepared.sha256)
    if existing and replace:
        history = repo.document_history(connection, int(existing["id"]))
        if history["questions"]:
            raise ImportError_(
                f"{prepared.path.name} already has {history['questions']} questions "
                f"and {history['attempts']} recorded answers. Re-chunking would "
                "discard both. Regenerate the chunks you want instead."
            )
        repo.delete_document(connection, int(existing["id"]))
        existing = None

    if existing:
        # The same book, somewhere else: renamed, moved, or handed over with a
        # bank that remembers the sender's folder. Re-importing it is what
        # someone tries first, so it is made to work -- the path is corrected
        # and the questions, pages and highlights are left exactly as they are,
        # because the hash says it really is the same file.
        if str(prepared.path) != existing["path"]:
            repo.relocate_document(connection, int(existing["id"]), prepared.path)
        return int(existing["id"]), False

    # One transaction for both. The document used to be committed first, so a
    # failure while writing the chunks left a document row with no chunks --
    # and because `sha256` is unique, that row answered "already imported" for
    # good, with nothing to generate from.
    document_id = repo.insert_document(
        connection,
        path=str(prepared.path),
        filename=prepared.path.name,
        sha256=prepared.sha256,
        page_count=prepared.page_count,
        language=prepared.language,
        commit=False,
    )
    try:
        repo.insert_chunks(connection, document_id, prepared.chunks)
    except Exception:
        connection.rollback()
        raise
    return document_id, True
