"""Imports a PDF: extract, chunk, store.

    python -m studyapp.ingest <pdf> [--dry-run] [--language cs]

The window can do this too (phase 4), but the CLI stays because `--dry-run` and a
scroll through the output is what you actually want when the chunking looks wrong.
The work itself lives in importer.py so both routes do exactly the same thing.
"""

import argparse
import sys
from pathlib import Path

from .. import config
from ..db import connection as db_connection
from . import importer, language


def preview(chunks, count: int = 5) -> str:
    """A few chunks spread across the document, to check the page ranges by eye.

    Evenly spaced rather than the first few: the opening pages of a textbook are
    a title page and a table of contents, which chunk unlike anything else in it.
    """
    if not chunks:
        return ""
    step = max(1, len(chunks) // count)
    lines = []
    for chunk in chunks[::step][:count]:
        opening = " ".join(chunk.text.split())[:110]
        lines.append(
            f"  #{chunk.ordinal:<4} pages {chunk.page_start + 1}-{chunk.page_end + 1:<5} "
            f"{chunk.word_count:>5} words  {chunk.title or '(no title)'}\n"
            f"        {opening}..."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m studyapp.ingest")
    parser.add_argument("pdf", type=Path, help="the PDF to import")
    parser.add_argument("--dry-run", action="store_true",
                        help="chunk and report, write nothing")
    parser.add_argument("--language", help="ISO code of the source language; "
                                           "overrides detection")
    parser.add_argument("--replace", action="store_true",
                        help="re-chunk a document already imported; refused once "
                             "it has questions")
    parser.add_argument("--min-words", type=int, default=config.CHUNK_MIN_WORDS)
    parser.add_argument("--max-words", type=int, default=config.CHUNK_MAX_WORDS)
    arguments = parser.parse_args(argv)

    print(f"Reading {arguments.pdf.name} ...")
    try:
        prepared = importer.prepare(arguments.pdf, arguments.min_words,
                                    arguments.max_words, arguments.language)
    except importer.ImportError_ as error:
        print(str(error), file=sys.stderr)
        return 2

    print()
    print(prepared.summary())
    print(f"source language: {language.name(prepared.language)}"
          + ("" if prepared.language else "  (undetected -- pass --language)"))
    print()
    print(preview(prepared.chunks))

    if arguments.dry_run:
        print("\nDry run, nothing written.")
        return 0

    db_connection.ensure_schema()
    database = db_connection.connect()
    try:
        document_id, is_new = importer.store(database, prepared, arguments.replace)
    except importer.ImportError_ as error:
        print(f"\n{error}", file=sys.stderr)
        return 2
    finally:
        database.close()

    if not is_new:
        print(f"\nAlready imported as document #{document_id}. Nothing to do.")
        return 0

    print(f"\nStored document #{document_id} with {len(prepared.chunks)} chunks, "
          "all pending.")
    print(f"Database: {config.db_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
