"""Moving a question bank between databases, as one JSON file.

JSON rather than a SQL dump, and the reason is not taste. A dump is a list of
INSERTs written against one shape of the schema: the next migration turns it into
a file that fails to load, and nothing in it says why. Worse, importing one means
running someone else's SQL against your own bank, where a single stray statement
reaches every table you own. A document is data, so it travels as data, and the
importer decides what to do with it.

What travels: the document's identity, its chunks, and its questions. Not the
attempts -- practice is personal, the bank is not -- and not the embeddings,
which are a cache that `embed/dedupe.py` can rebuild.

What does NOT travel is the PDF itself. Every question carries a page number and
a rectangle on that page, and both mean nothing without the file they were found
in. The export records the document's SHA-256 so the other side can check it has
the same one; until it does, the questions are there and the source pane is not.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import repo

# Bumped when the shape below changes in a way an older reader cannot handle.
# Readers refuse a version they do not know rather than guessing at it.
FORMAT = 1
MARKER = "studyapp_export"


class TransferError(Exception):
    """The file is not a StudyApp export, or not one this version can read."""


@dataclass
class ImportReport:
    """What an import did, in the terms the confirmation needs to quote back."""

    documents_added: int = 0
    documents_matched: int = 0
    chunks_added: int = 0
    chunks_matched: int = 0
    questions_added: int = 0
    questions_skipped: int = 0
    filenames: list[str] = field(default_factory=list)
    needs_pdf: list[str] = field(default_factory=list)


# --- writing ----------------------------------------------------------------


def _question_payload(row: sqlite3.Row) -> dict:
    """One question, with everything that is not derivable from the rest.

    `embedding` is left out on purpose: it is a cache, it is large, and the
    model that produced it may not be the one the far side would use.
    """
    return {
        "type": row["type"],
        "question": row["question"],
        "options": json.loads(row["options_json"]) if row["options_json"] else None,
        "correct_index": row["correct_index"],
        "correct_text": row["correct_text"],
        "explanation": row["explanation"],
        "source_quote": row["source_quote"],
        "quote_score": row["quote_score"],
        "page": row["page"],
        "rects": json.loads(row["rects_json"]) if row["rects_json"] else None,
        "is_active": bool(row["is_active"]),
        "starred": bool(row["starred"]),
        # Carried because it is the mark that protects a hand-corrected question
        # from the next regeneration. Losing it on the way over would quietly
        # put the correction back in the firing line.
        "edited_at": row["edited_at"],
        "created_at": row["created_at"],
    }


def export_payload(connection: sqlite3.Connection,
                   document_id: int | None = None) -> dict:
    """The whole export, as the object that gets written out.

    `document_id=None` takes every document, which is what the picker's "All
    documents" means.
    """
    documents = (repo.list_documents(connection) if document_id is None
                 else [row for row in repo.list_documents(connection)
                       if row["id"] == document_id])

    payload: dict[str, Any] = {
        MARKER: FORMAT,
        "exported_at": repo.utcnow(),
        "documents": [],
    }

    for document in documents:
        chunks = repo.all_chunks(connection, document["id"])
        chunk_payloads = []
        for chunk in chunks:
            questions = repo.chunk_questions(connection, chunk["id"])
            # A chunk nothing was ever asked about carries no questions and no
            # meaning on the far side, so it does not travel.
            if not questions:
                continue
            chunk_payloads.append({
                "ordinal": chunk["ordinal"],
                "page_start": chunk["page_start"],
                "page_end": chunk["page_end"],
                "title": chunk["title"],
                # The passage itself, so the far side can regenerate from it and
                # so a quote can still be checked against its source.
                "text": chunk["text"],
                "sha256": chunk["sha256"],
                "status": chunk["status"],
                "archived": bool(chunk["archived"]),
                "questions": [_question_payload(row) for row in questions],
            })

        if not chunk_payloads:
            continue
        payload["documents"].append({
            "filename": document["filename"],
            # The identity that matters. Filenames collide and get renamed; this
            # is what says the far side is looking at the same book.
            "sha256": document["sha256"],
            "page_count": document["page_count"],
            "language": document["language"],
            "chunks": chunk_payloads,
        })

    return payload


def write(connection: sqlite3.Connection, path: str | Path,
          document_id: int | None = None) -> dict:
    """Writes an export and reports what went into it."""
    payload = export_payload(connection, document_id)
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "documents": len(payload["documents"]),
        "chunks": sum(len(d["chunks"]) for d in payload["documents"]),
        "questions": sum(len(c["questions"])
                         for d in payload["documents"] for c in d["chunks"]),
    }


# --- reading ----------------------------------------------------------------


def read(path: str | Path) -> dict:
    """Loads an export, or says why the file cannot be one."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise TransferError(f"This is not a JSON file: {error}") from error
    except OSError as error:
        raise TransferError(str(error)) from error

    if not isinstance(payload, dict) or MARKER not in payload:
        raise TransferError(
            "This JSON file is not a StudyApp export -- it has no "
            f"'{MARKER}' marker.")

    version = payload[MARKER]
    if not isinstance(version, int) or version > FORMAT:
        raise TransferError(
            f"This export is version {version}; this copy of StudyApp reads up "
            f"to version {FORMAT}. Update the app and try again.")

    if not isinstance(payload.get("documents"), list):
        raise TransferError("The export has no documents in it.")
    _check(payload)
    return payload


# What `apply` goes on to read without looking, and the type each one has to be.
# Checked here, once, so that a file written by hand or damaged in transit comes
# back as a sentence rather than as a KeyError three levels down -- which under
# pythonw is a traceback nobody sees and a dialog that never appears.
_DOCUMENT_FIELDS = {"sha256": str}
_CHUNK_FIELDS = {"sha256": str, "text": str, "ordinal": int,
                 "page_start": int, "page_end": int}
_QUESTION_FIELDS = {"type": str, "question": str, "source_quote": str}


def _check_fields(item, fields: dict, what: str, where: str) -> None:
    if not isinstance(item, dict):
        raise TransferError(f"{where}: expected {what} details, found "
                            f"{type(item).__name__}.")
    for name, wanted in fields.items():
        if name not in item:
            raise TransferError(f"{where}: this {what} has no '{name}'.")
        value = item[name]
        # bool is an int to Python and never a page number to anyone else.
        if not isinstance(value, wanted) or isinstance(value, bool):
            raise TransferError(
                f"{where}: '{name}' should be {wanted.__name__}, "
                f"found {type(value).__name__}.")


def _check(payload: dict) -> None:
    """Rejects an export `apply` could not survive, naming where it went wrong."""
    for position, document in enumerate(payload["documents"], start=1):
        where = f"document {position}"
        _check_fields(document, _DOCUMENT_FIELDS, "document", where)
        name = document.get("filename") or where

        chunks = document.get("chunks", [])
        if not isinstance(chunks, list):
            raise TransferError(f"{name}: 'chunks' should be a list, "
                                f"found {type(chunks).__name__}.")
        for index, chunk in enumerate(chunks, start=1):
            chunk_where = f"{name}, chunk {index}"
            _check_fields(chunk, _CHUNK_FIELDS, "chunk", chunk_where)

            questions = chunk.get("questions", [])
            if not isinstance(questions, list):
                raise TransferError(
                    f"{chunk_where}: 'questions' should be a list, "
                    f"found {type(questions).__name__}.")
            for number, question in enumerate(questions, start=1):
                _check_fields(question, _QUESTION_FIELDS, "question",
                              f"{chunk_where}, question {number}")
                if question["type"] not in ("mcq", "open"):
                    raise TransferError(
                        f"{chunk_where}, question {number}: type is "
                        f"{question['type']!r}, not 'mcq' or 'open'.")
                options = question.get("options")
                if options is not None and not isinstance(options, list):
                    raise TransferError(
                        f"{chunk_where}, question {number}: 'options' should be "
                        f"a list, found {type(options).__name__}.")


def describe(payload: dict) -> dict:
    """What the file holds, for a confirmation shown before anything is written."""
    documents = payload.get("documents", [])
    return {
        "documents": len(documents),
        "questions": sum(len(chunk["questions"])
                         for document in documents
                         for chunk in document.get("chunks", [])),
        "filenames": [document.get("filename", "?") for document in documents],
    }


# --- importing --------------------------------------------------------------


def _fingerprint(question_text: str) -> str:
    return " ".join(question_text.split()).casefold()


def apply(connection: sqlite3.Connection, payload: dict) -> ImportReport:
    """Writes an export into this database, adding only what is not already here.

    Matching is by content, never by row id: the ids in an export belong to the
    database it came from and mean nothing here. A document is the same document
    when its PDF hashes the same, a chunk when its text does, and a question when
    it reads the same within that chunk.

    That makes importing the same file twice harmless, which matters because the
    obvious way to find out whether an import worked is to do it again.
    """
    report = ImportReport()

    for document in payload.get("documents", []):
        existing = repo.get_document_by_sha(connection, document["sha256"])
        if existing is None:
            # The path is the sender's and will not resolve here. It is stored
            # anyway so the filename shows in the picker, where Locate PDF is
            # the next thing to click.
            document_id = repo.insert_document(
                connection,
                path=document.get("filename", ""),
                filename=document.get("filename", ""),
                sha256=document["sha256"],
                page_count=document.get("page_count", 0),
                language=document.get("language"),
            )
            report.documents_added += 1
        else:
            document_id = existing["id"]
            report.documents_matched += 1
        report.filenames.append(document.get("filename", "?"))
        if not repo.document_is_present(connection, document_id):
            report.needs_pdf.append(document.get("filename", "?"))

        for chunk in document.get("chunks", []):
            row = repo.get_chunk_by_sha(connection, document_id, chunk["sha256"])
            if row is None:
                chunk_id = repo.insert_imported_chunk(connection, document_id, chunk)
                report.chunks_added += 1
            else:
                chunk_id = row["id"]
                report.chunks_matched += 1

            here = {_fingerprint(text)
                    for text in repo.question_texts(connection, chunk_id)}

            fresh = []
            for question in chunk.get("questions", []):
                if _fingerprint(question["question"]) in here:
                    report.questions_skipped += 1
                    continue
                here.add(_fingerprint(question["question"]))
                fresh.append(question)

            if fresh:
                report.questions_added += repo.insert_imported_questions(
                    connection, chunk_id, fresh)

    return report
