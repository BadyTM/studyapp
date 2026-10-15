"""Every query in the application.

Keeping the SQL in one file is what makes the schema changeable at all: when a
column moves, this is the only place that has to follow. Callers pass in the
connection so the caller owns the transaction -- and, during generation, so that
there is provably only one writer.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


def utcnow() -> str:
    """The one timestamp format used everywhere: ISO 8601 in UTC.

    Localised only when displayed. Mixed zones in a database that has no datetime
    type turn into a sorting bug nobody finds for a month.
    """
    return datetime.now(timezone.utc).isoformat()


# --- documents --------------------------------------------------------------


def get_document_by_sha(connection: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM documents WHERE sha256 = ?", (sha256,)
    ).fetchone()


def get_document(connection: sqlite3.Connection, document_id: int) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()


def list_documents(connection: sqlite3.Connection,
                   generatable_only: bool = False) -> list[sqlite3.Row]:
    """Every document, or only those still loaded for generation.

    A document whose chunks have all been archived is finished with as far as the
    Generate tab is concerned, but its questions are still in the bank and still
    need it for their page previews -- so it stays in the database and keeps
    answering the other views.
    """
    sql = "SELECT * FROM documents"
    if generatable_only:
        sql += (" WHERE id IN (SELECT DISTINCT document_id FROM chunks"
                "             WHERE archived = 0)")
    return connection.execute(sql + " ORDER BY created_at DESC").fetchall()


def insert_document(
    connection: sqlite3.Connection,
    path: str,
    filename: str,
    sha256: str,
    page_count: int,
    language: str | None = None,
    commit: bool = True,
) -> int:
    """Adds a document row.

    `commit=False` leaves it uncommitted so the caller can add its chunks in the
    same transaction. An import that committed the document and then failed on
    the chunks left a document with nothing in it -- and `sha256` is unique, so
    its row answered "already imported, nothing to do" from then on.
    """
    cursor = connection.execute(
        """INSERT INTO documents (path, filename, sha256, page_count, language, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (path, filename, sha256, page_count, language, utcnow()),
    )
    if commit:
        connection.commit()
    return int(cursor.lastrowid)


# --- chunks -----------------------------------------------------------------


def insert_chunks(connection: sqlite3.Connection, document_id: int, chunks: Iterable) -> int:
    """Stores a document's chunks, all starting out `pending`.

    `chunks` are ingest.chunker.Chunk objects; this is the only place that knows
    how one maps onto a row.
    """
    now = utcnow()
    rows = [
        (document_id, chunk.ordinal, chunk.page_start, chunk.page_end,
         chunk.title, chunk.text, chunk.sha256, "pending", now)
        for chunk in chunks
    ]
    connection.executemany(
        """INSERT INTO chunks
               (document_id, ordinal, page_start, page_end, title, text,
                sha256, status, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    connection.commit()
    return len(rows)


def get_chunk(connection: sqlite3.Connection, chunk_id: int) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()


def get_pending_chunks(
    connection: sqlite3.Connection,
    document_id: int | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Chunks still waiting to be generated from.

    `running` counts as pending: a run interrupted mid-flight left rows in that
    state and its work was never written, so it has to be picked up again.
    """
    sql = ("SELECT * FROM chunks "
           "WHERE archived = 0 AND status IN ('pending', 'running')")
    parameters: list[Any] = []
    if document_id is not None:
        sql += " AND document_id = ?"
        parameters.append(document_id)
    sql += " ORDER BY ordinal"
    if limit is not None:
        sql += " LIMIT ?"
        parameters.append(limit)
    return connection.execute(sql, parameters).fetchall()


def list_chunks(connection: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    """Chunks with their question counts, for the generation table."""
    return connection.execute(
        """SELECT c.*, COUNT(q.id) AS question_count
           FROM chunks c
           LEFT JOIN questions q ON q.chunk_id = c.id AND q.is_active = 1
           WHERE c.document_id = ? AND c.archived = 0
           GROUP BY c.id
           ORDER BY c.ordinal""",
        (document_id,),
    ).fetchall()


def set_chunk_status(
    connection: sqlite3.Connection,
    chunk_id: int,
    status: str,
    error: str | None = None,
) -> None:
    connection.execute(
        "UPDATE chunks SET status = ?, error = ?, updated_at = ? WHERE id = ?",
        (status, error, utcnow(), chunk_id),
    )
    connection.commit()


def reset_chunks(connection: sqlite3.Connection, chunk_ids: Sequence[int]) -> None:
    """Queues chunks for regeneration.

    Existing questions are left alone here; whether they are replaced is decided
    when the new ones arrive, so hand-edited ones can be kept.
    """
    connection.executemany(
        "UPDATE chunks SET status = 'pending', error = NULL, updated_at = ? WHERE id = ?",
        [(utcnow(), chunk_id) for chunk_id in chunk_ids],
    )
    connection.commit()


def chunk_status_counts(connection: sqlite3.Connection, document_id: int) -> dict[str, int]:
    rows = connection.execute(
        "SELECT status, COUNT(*) AS n FROM chunks "
        "WHERE document_id = ? AND archived = 0 GROUP BY status",
        (document_id,),
    ).fetchall()
    return {row["status"]: row["n"] for row in rows}


# --- questions --------------------------------------------------------------


def insert_questions(
    connection: sqlite3.Connection,
    chunk_id: int,
    questions: Sequence[dict],
) -> int:
    """Writes validated questions for one chunk.

    Called only from the single writing thread -- workers hand their results over
    by signal precisely so that this never runs concurrently.
    """
    now = utcnow()
    rows = [
        (
            chunk_id,
            question["type"],
            question["question"],
            json.dumps(question["options"], ensure_ascii=False) if question.get("options") else None,
            question.get("correct_index"),
            question.get("correct_text"),
            question.get("explanation"),
            question["source_quote"],
            question.get("quote_score"),
            question.get("page"),
            json.dumps(question["rects"], ensure_ascii=False) if question.get("rects") else None,
            now,
        )
        for question in questions
    ]
    connection.executemany(
        """INSERT INTO questions
               (chunk_id, type, question, options_json, correct_index, correct_text,
                explanation, source_quote, quote_score, page, rects_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    connection.commit()
    return len(rows)


def deactivate_unedited_questions(connection: sqlite3.Connection, chunk_id: int) -> int:
    """Retires a chunk's generated questions before it is regenerated.

    Hand-edited ones survive: `edited_at` records that someone read the question
    and fixed it, and a regeneration run silently overwriting that is the fastest
    way to stop trusting the manual pass.
    """
    cursor = connection.execute(
        """UPDATE questions SET is_active = 0
           WHERE chunk_id = ? AND edited_at IS NULL AND is_active = 1""",
        (chunk_id,),
    )
    connection.commit()
    return cursor.rowcount


def count_questions(connection: sqlite3.Connection, active_only: bool = True,
                    document_id: int | None = None) -> int:
    """How many questions there are, optionally within one document."""
    where = []
    parameters: list[Any] = []
    if active_only:
        where.append("is_active = 1")
    if document_id is not None:
        where.append("""chunk_id IN (SELECT id FROM chunks WHERE document_id = ?)""")
        parameters.append(document_id)
    sql = "SELECT COUNT(*) AS n FROM questions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return int(connection.execute(sql, parameters).fetchone()["n"])


# --- picking questions to practise ------------------------------------------

# Everything the quiz needs about a question, including where it came from. The
# join is what supplies the chapter title and the page range; the quiz never
# reads a chunk itself.
QUESTION_SELECT = """
    SELECT q.*, c.title AS chapter, c.page_start, c.page_end, d.path AS pdf_path,
           d.sha256 AS document_sha, d.page_count
    FROM questions q
    JOIN chunks c ON c.id = q.chunk_id
    JOIN documents d ON d.id = c.document_id
"""


def list_chapters(connection: sqlite3.Connection,
                  document_id: int | None = None) -> list[sqlite3.Row]:
    """Chapter titles that actually have questions behind them.

    Titles with nothing to ask are left out of the picker: offering a chapter
    that yields an empty session is worse than not offering it.

    `document_id=None` means every document. Two books can use the same chapter
    title, and then one row comes back covering both -- which is right for a
    filter that is itself about titles rather than about documents, and is why
    the page number is a MIN rather than the chapter's own.
    """
    sql = """SELECT c.title, COUNT(q.id) AS question_count,
                    MIN(c.page_start) AS page_start
             FROM chunks c
             JOIN questions q ON q.chunk_id = c.id AND q.is_active = 1
             WHERE c.title IS NOT NULL"""
    parameters: list[Any] = []
    if document_id is not None:
        sql += " AND c.document_id = ?"
        parameters.append(document_id)
    sql += """ GROUP BY c.title
               HAVING question_count > 0
               ORDER BY page_start"""
    return connection.execute(sql, parameters).fetchall()


MISTAKES = """q.id IN (
                   SELECT a.question_id FROM attempts a
                   WHERE a.id = (SELECT MAX(id) FROM attempts
                                 WHERE question_id = a.question_id)
                     AND a.is_correct = 0
               )"""


def mistakes_count(connection: sqlite3.Connection,
                   document_id: int | None = None) -> int:
    """How many questions the mistakes queue is holding.

    Counted rather than fetched. This used to be `len(pick_questions(...))`,
    which asks the same question but builds every row to throw all of them
    away -- and shuffles them on the way, because that is what picking a session
    needs and counting does not. On a bank of 20,000 questions that was 7,545
    rows and 54ms for a number in a label.
    """
    sql = f"""SELECT COUNT(*) FROM questions q
              JOIN chunks c ON c.id = q.chunk_id
              WHERE q.is_active = 1 AND {MISTAKES}"""
    parameters: list[Any] = []
    if document_id is not None:
        sql += " AND c.document_id = ?"
        parameters.append(document_id)
    return connection.execute(sql, parameters).fetchone()[0]


def pick_questions(
    connection: sqlite3.Connection,
    types: Sequence[str] = ("mcq", "open"),
    source: str = "all",
    limit: int | None = 20,
    document_id: int | None = None,
    chapters: Sequence[str] | None = None,
) -> list[sqlite3.Row]:
    """The questions for one session, already shuffled.

    `source="mistakes"` is derived from `attempts` rather than kept in a table of
    its own. A separate table would be a second copy of the truth and would drift
    from it; this way a question leaves the queue the moment it is answered
    correctly, with nothing to keep in step.
    """
    where = ["q.is_active = 1"]
    parameters: list[Any] = []

    kinds = list(types) or ["mcq", "open"]
    where.append(f"q.type IN ({','.join('?' * len(kinds))})")
    parameters.extend(kinds)

    if document_id is not None:
        where.append("c.document_id = ?")
        parameters.append(document_id)

    if source == "mistakes":
        # The newest attempt at each question decides. Answer it right and it
        # drops out by itself. Shared with `mistakes_count`, which counts the
        # same queue -- two spellings of one rule would drift.
        where.append(MISTAKES)
    elif source == "starred":
        where.append("q.starred = 1")
    elif source == "chapter":
        if not chapters:
            return []
        where.append(f"c.title IN ({','.join('?' * len(chapters))})")
        parameters.extend(chapters)
    elif source == "unseen":
        where.append("q.id NOT IN (SELECT question_id FROM attempts)")

    sql = f"{QUESTION_SELECT} WHERE {' AND '.join(where)} ORDER BY RANDOM()"
    if limit is not None:
        sql += " LIMIT ?"
        parameters.append(limit)
    return connection.execute(sql, parameters).fetchall()


def get_question(connection: sqlite3.Connection, question_id: int) -> sqlite3.Row | None:
    return connection.execute(
        f"{QUESTION_SELECT} WHERE q.id = ?", (question_id,)
    ).fetchone()


def existing_question_ids(connection: sqlite3.Connection,
                          question_ids: Sequence[int]) -> set[int]:
    """Which of these questions are still in the database.

    A running session holds its questions in memory, so anything that deletes
    them -- removing their document, purging retired ones -- leaves it holding
    rows that are gone. Writing an attempt against one of those fails on the
    foreign key, which under pythonw is an exception nobody ever sees.
    """
    if not question_ids:
        return set()
    marks = ",".join("?" * len(question_ids))
    rows = connection.execute(
        f"SELECT id FROM questions WHERE id IN ({marks})", tuple(question_ids))
    return {int(row["id"]) for row in rows}


def set_starred(connection: sqlite3.Connection, question_id: int, starred: bool) -> None:
    connection.execute(
        "UPDATE questions SET starred = ? WHERE id = ?", (1 if starred else 0, question_id)
    )
    connection.commit()


# --- sessions and attempts --------------------------------------------------


def start_session(
    connection: sqlite3.Connection,
    types: Sequence[str],
    source: str,
    filters: dict | None = None,
) -> int:
    cursor = connection.execute(
        """INSERT INTO sessions (started_at, types_json, source, filter_json)
           VALUES (?, ?, ?, ?)""",
        (utcnow(), json.dumps(list(types)), source,
         json.dumps(filters or {}, ensure_ascii=False)),
    )
    connection.commit()
    return int(cursor.lastrowid)


def end_session(connection: sqlite3.Connection, session_id: int) -> None:
    """Closes a session.

    A session left open by closing the app stays open, and the next one starts
    fresh. Nothing is lost by that: attempts are written one at a time as they
    happen, not in a batch at the end.
    """
    connection.execute(
        "UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
        (utcnow(), session_id),
    )
    connection.commit()


def record_attempt(
    connection: sqlite3.Connection,
    session_id: int | None,
    question_id: int,
    is_correct: bool,
    user_answer: str | None = None,
    self_graded: bool = False,
    revealed_ms: int | None = None,
) -> int:
    cursor = connection.execute(
        """INSERT INTO attempts (session_id, question_id, answered_at, user_answer,
                                 is_correct, self_graded, revealed_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session_id, question_id, utcnow(), user_answer,
         1 if is_correct else 0, 1 if self_graded else 0, revealed_ms),
    )
    connection.commit()
    return int(cursor.lastrowid)


# Attempts reach a document through two joins, and four of the figures below
# need the same one. Written once, as a fragment rather than a view, because the
# surrounding queries differ in what they group by.
_ATTEMPTS_OF_DOCUMENT = """
    question_id IN (SELECT q.id FROM questions q
                    JOIN chunks c ON c.id = q.chunk_id
                    WHERE c.document_id = ?)
"""


def overall_accuracy(connection: sqlite3.Connection,
                     document_id: int | None = None) -> tuple[int, int]:
    """Correct answers and total attempts, over the whole history.

    `document_id=None` is every document, which is what the Progress tab shows
    until a book is picked.
    """
    sql = "SELECT COUNT(*) AS n, COALESCE(SUM(is_correct), 0) AS correct FROM attempts"
    parameters: list[Any] = []
    if document_id is not None:
        sql += f" WHERE {_ATTEMPTS_OF_DOCUMENT}"
        parameters.append(document_id)
    row = connection.execute(sql, parameters).fetchone()
    return int(row["correct"]), int(row["n"])


# --- embeddings and deduplication -------------------------------------------


def questions_for_embedding(connection: sqlite3.Connection,
                            only_missing: bool = False) -> list[sqlite3.Row]:
    """Active questions, with the text the deduplication compares.

    The question text carries the meaning; the quote is only there to decide
    which of two duplicates to keep.
    """
    sql = """SELECT id, question, source_quote, embedding
             FROM questions WHERE is_active = 1"""
    if only_missing:
        sql += " AND embedding IS NULL"
    return connection.execute(sql + " ORDER BY id").fetchall()


def store_embeddings(connection: sqlite3.Connection,
                     embeddings: Sequence[tuple[int, bytes]]) -> int:
    connection.executemany(
        "UPDATE questions SET embedding = ? WHERE id = ?",
        [(blob, question_id) for question_id, blob in embeddings],
    )
    connection.commit()
    return len(embeddings)


# --- statistics -------------------------------------------------------------


def accuracy_by_chapter(connection: sqlite3.Connection,
                        document_id: int | None = None) -> list[sqlite3.Row]:
    """How each chapter is going, newest attempt per question not required.

    Every attempt counts here, not just the last one: the point is to find the
    chapter that keeps going wrong, and a chapter answered wrong three times and
    right once is exactly that.
    """
    sql = """SELECT c.title AS chapter,
                  MIN(c.page_start) AS page_start,
                  COUNT(a.id) AS attempts,
                  COALESCE(SUM(a.is_correct), 0) AS correct,
                  COUNT(DISTINCT q.id) AS questions
           FROM chunks c
           JOIN questions q ON q.chunk_id = c.id AND q.is_active = 1
           LEFT JOIN attempts a ON a.question_id = q.id
           WHERE c.title IS NOT NULL{extra}
           GROUP BY c.title
           ORDER BY page_start"""
    parameters: list[Any] = []
    extra = ""
    if document_id is not None:
        extra = " AND c.document_id = ?"
        parameters.append(document_id)
    return connection.execute(sql.format(extra=extra), parameters).fetchall()


def session_history(connection: sqlite3.Connection, limit: int = 20,
                    document_id: int | None = None) -> list[sqlite3.Row]:
    """Recent sessions and how they went.

    Filtered by what a session actually asked about rather than by what it was
    set up to draw from: a session over the whole bank counts for every book it
    touched, which is the honest answer to "how have I done on this one".
    """
    sql = """SELECT s.*,
                    COUNT(a.id) AS answered,
                    COALESCE(SUM(a.is_correct), 0) AS correct
             FROM sessions s
             LEFT JOIN attempts a ON a.session_id = s.id"""
    parameters: list[Any] = []
    if document_id is not None:
        sql += f" AND a.{_ATTEMPTS_OF_DOCUMENT.strip()}"
        parameters.append(document_id)
    sql += """ GROUP BY s.id
               HAVING answered > 0
               ORDER BY s.id DESC
               LIMIT ?"""
    parameters.append(limit)
    return connection.execute(sql, parameters).fetchall()


def bank_totals(connection: sqlite3.Connection,
                document_id: int | None = None) -> dict[str, int]:
    """One row of counters for the top of the statistics view."""
    where = ""
    parameters: list[Any] = []
    if document_id is not None:
        where = (" WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id = ?)")
        parameters.append(document_id)
    row = connection.execute(
        """SELECT
             COUNT(*) FILTER (WHERE is_active = 1) AS active,
             COUNT(*) FILTER (WHERE is_active = 0) AS retired,
             COUNT(*) FILTER (WHERE is_active = 1 AND type = 'mcq') AS mcq,
             COUNT(*) FILTER (WHERE is_active = 1 AND type = 'open') AS open,
             COUNT(*) FILTER (WHERE is_active = 1 AND starred = 1) AS starred
           FROM questions""" + where, parameters
    ).fetchone()
    return {key: int(row[key]) for key in row.keys()}


def practised_count(connection: sqlite3.Connection,
                    document_id: int | None = None) -> int:
    """How many distinct active questions have ever been answered."""
    sql = """SELECT COUNT(DISTINCT a.question_id) AS n
             FROM attempts a
             JOIN questions q ON q.id = a.question_id
             JOIN chunks c ON c.id = q.chunk_id
             WHERE q.is_active = 1"""
    parameters: list[Any] = []
    if document_id is not None:
        sql += " AND c.document_id = ?"
        parameters.append(document_id)
    return int(connection.execute(sql, parameters).fetchone()["n"])


# --- managing the bank ------------------------------------------------------

# What may be changed by hand. `source_quote`, `page` and `rects_json` are not
# here on purpose: they are derived from the PDF, not opinions about the
# question. A quote edited by hand no longer matches the text it was found in,
# which silently breaks the highlight and hides the fact that the model
# paraphrased. A question with a bad quote is a bad question -- retire it or
# regenerate the chunk.
EDITABLE_FIELDS = ("question", "options_json", "correct_index", "correct_text",
                   "explanation", "type")


def search_questions(
    connection: sqlite3.Connection,
    text: str | None = None,
    types: Sequence[str] | None = None,
    chapter: str | None = None,
    state: str = "active",          # active | inactive | all | review
    starred_only: bool = False,
    limit: int = 1000,
    document_id: int | None = None,
) -> list[sqlite3.Row]:
    """The bank, filtered. Backs the management table.

    `document_id=None` is every document, which is what the review count wants:
    a quote that could not be placed is worth knowing about whichever book it
    came from.
    """
    where: list[str] = []
    parameters: list[Any] = []

    if document_id is not None:
        where.append("c.document_id = ?")
        parameters.append(document_id)

    if state == "active":
        where.append("q.is_active = 1")
    elif state == "inactive":
        where.append("q.is_active = 0")
    elif state == "review":
        # The useful preset: a quote that could not be placed in the PDF usually
        # means the model paraphrased, so these are suspect on content too.
        where.append("q.is_active = 1")
        where.append("(q.rects_json IS NULL OR q.page IS NULL OR q.quote_score < 0.95)")

    if starred_only:
        where.append("q.starred = 1")
    if types:
        where.append(f"q.type IN ({','.join('?' * len(types))})")
        parameters.extend(types)
    if chapter:
        where.append("c.title = ?")
        parameters.append(chapter)
    if text:
        where.append("(q.question LIKE ? OR q.explanation LIKE ? "
                     "OR q.correct_text LIKE ? OR q.options_json LIKE ?)")
        parameters.extend([f"%{text}%"] * 4)

    sql = QUESTION_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY c.page_start, q.id LIMIT ?"
    parameters.append(limit)
    return connection.execute(sql, parameters).fetchall()


def update_question(connection: sqlite3.Connection, question_id: int, **fields) -> bool:
    """Saves a hand edit, and records that it was one.

    `edited_at` is what protects the edit from being thrown away the next time
    its chunk is regenerated, so it is set here rather than left to the caller to
    remember.
    """
    changes = {key: value for key, value in fields.items() if key in EDITABLE_FIELDS}
    if not changes:
        return False

    assignments = ", ".join(f"{key} = ?" for key in changes)
    connection.execute(
        f"UPDATE questions SET {assignments}, edited_at = ? WHERE id = ?",
        (*changes.values(), utcnow(), question_id),
    )
    connection.commit()
    return True


def set_active(connection: sqlite3.Connection, question_ids: Sequence[int],
               active: bool) -> int:
    """Retires or restores questions.

    Deliberately not an edit: `edited_at` marks a corrected question, and
    retiring one is not a correction. Marking it as such would stop the chunk
    ever regenerating it.
    """
    if not question_ids:
        return 0
    connection.executemany(
        "UPDATE questions SET is_active = ? WHERE id = ?",
        [(1 if active else 0, question_id) for question_id in question_ids],
    )
    connection.commit()
    return len(question_ids)


def set_starred_many(connection: sqlite3.Connection, question_ids: Sequence[int],
                     starred: bool) -> int:
    if not question_ids:
        return 0
    connection.executemany(
        "UPDATE questions SET starred = ? WHERE id = ?",
        [(1 if starred else 0, question_id) for question_id in question_ids],
    )
    connection.commit()
    return len(question_ids)


def purgeable_count(connection: sqlite3.Connection) -> int:
    return len(purgeable_questions(connection))


def purgeable_questions(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Retired questions that nothing has ever been answered against.

    Only these can actually be deleted. A retired question with attempts behind
    it is history: deleting it would either break the foreign key or quietly take
    a piece of the record with it.
    """
    return connection.execute(
        """SELECT * FROM questions
           WHERE is_active = 0
             AND id NOT IN (SELECT DISTINCT question_id FROM attempts)"""
    ).fetchall()


def purge_inactive(connection: sqlite3.Connection) -> int:
    """Deletes retired questions that have no history. Everything else stays."""
    cursor = connection.execute(
        """DELETE FROM questions
           WHERE is_active = 0
             AND id NOT IN (SELECT DISTINCT question_id FROM attempts)"""
    )
    connection.commit()
    return cursor.rowcount


# --- moving a bank between databases ----------------------------------------
#
# transfer.py builds and reads the export; the SQL for it lives here with every
# other query, so a schema change still has one place to follow.


def all_chunks(connection: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    """Every chunk of a document, archived ones included."""
    return connection.execute(
        "SELECT * FROM chunks WHERE document_id = ? ORDER BY ordinal",
        (document_id,)).fetchall()


def chunk_questions(connection: sqlite3.Connection, chunk_id: int) -> list[sqlite3.Row]:
    """Every question of a chunk, retired ones included."""
    return connection.execute(
        "SELECT * FROM questions WHERE chunk_id = ? ORDER BY id",
        (chunk_id,)).fetchall()


def get_chunk_by_sha(connection: sqlite3.Connection, document_id: int,
                     sha256: str) -> sqlite3.Row | None:
    """The chunk with this text, within one document."""
    return connection.execute(
        "SELECT * FROM chunks WHERE document_id = ? AND sha256 = ?",
        (document_id, sha256)).fetchone()


def question_texts(connection: sqlite3.Connection, chunk_id: int) -> list[str]:
    """What a chunk already asks, for deciding whether an import adds anything."""
    return [row["question"] for row in connection.execute(
        "SELECT question FROM questions WHERE chunk_id = ?", (chunk_id,))]


def insert_imported_chunk(connection: sqlite3.Connection, document_id: int,
                          chunk: dict) -> int:
    """Writes a chunk that came from an export, keeping its own status."""
    cursor = connection.execute(
        """INSERT INTO chunks (document_id, ordinal, page_start, page_end, title,
                               text, sha256, status, archived, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (document_id, chunk["ordinal"], chunk["page_start"], chunk["page_end"],
         chunk.get("title"), chunk["text"], chunk["sha256"],
         chunk.get("status", "done"), 1 if chunk.get("archived") else 0, utcnow()))
    connection.commit()
    return int(cursor.lastrowid)


def insert_imported_questions(connection: sqlite3.Connection, chunk_id: int,
                              questions: Sequence[dict]) -> int:
    """Writes questions that came from an export.

    Separate from `insert_questions` because these arrive with a history:
    whether they are retired, whether they are starred, and whether someone
    corrected them by hand. That last one is the reason -- losing `edited_at`
    would put a correction back in the firing line of the next regeneration.
    """
    rows = [
        (chunk_id, question["type"], question["question"],
         json.dumps(question["options"], ensure_ascii=False)
         if question.get("options") else None,
         question.get("correct_index"),
         question.get("correct_text"),
         question.get("explanation"),
         question["source_quote"],
         question.get("quote_score"),
         question.get("page"),
         json.dumps(question["rects"], ensure_ascii=False)
         if question.get("rects") else None,
         1 if question.get("is_active", True) else 0,
         1 if question.get("starred") else 0,
         question.get("edited_at"),
         question.get("created_at") or utcnow())
        for question in questions
    ]
    connection.executemany(
        """INSERT INTO questions
               (chunk_id, type, question, options_json, correct_index,
                correct_text, explanation, source_quote, quote_score, page,
                rects_json, is_active, starred, edited_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows)
    connection.commit()
    return len(rows)


# --- re-importing -----------------------------------------------------------


def document_history(connection: sqlite3.Connection, document_id: int) -> dict[str, int]:
    """What would be lost by discarding a document: questions and answers."""
    row = connection.execute(
        """SELECT
             (SELECT COUNT(*) FROM questions q JOIN chunks c ON c.id = q.chunk_id
              WHERE c.document_id = ?) AS questions,
             (SELECT COUNT(*) FROM attempts a
              JOIN questions q ON q.id = a.question_id
              JOIN chunks c ON c.id = q.chunk_id
              WHERE c.document_id = ?) AS attempts""",
        (document_id, document_id),
    ).fetchone()
    return {"questions": int(row["questions"]), "attempts": int(row["attempts"])}


def delete_document(connection: sqlite3.Connection, document_id: int) -> None:
    """Removes a document and everything hanging off it.

    Only ever right when there is nothing worth keeping -- re-chunking a document
    that already has answers behind it would throw away the history along with
    the chunks. `document_history` is what the caller checks first.
    """
    connection.execute(
        """DELETE FROM attempts WHERE question_id IN
             (SELECT q.id FROM questions q JOIN chunks c ON c.id = q.chunk_id
              WHERE c.document_id = ?)""",
        (document_id,),
    )
    connection.execute(
        """DELETE FROM questions WHERE chunk_id IN
             (SELECT id FROM chunks WHERE document_id = ?)""",
        (document_id,),
    )
    connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
    connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    connection.commit()


def unload_document(connection: sqlite3.Connection, document_id: int) -> dict[str, int]:
    """Takes a document out of the generation list without losing its questions.

    Each chunk goes one of two ways. One that produced questions is archived:
    the questions reach their chapter, page, highlight rectangles and the PDF
    through it, so deleting it would quietly break every one of them. One that
    produced nothing has nothing to protect and is deleted.

    The document row itself survives as long as any chunk does, because the
    questions still need its path to show the page. Only when every chunk was
    deleted is the document gone too -- and the caller can tell from
    `document_deleted` whether the blurred pages are still needed.
    """
    with_questions = connection.execute(
        """SELECT DISTINCT c.id FROM chunks c
           JOIN questions q ON q.chunk_id = c.id
           WHERE c.document_id = ? AND c.archived = 0""",
        (document_id,),
    ).fetchall()
    keep = [row["id"] for row in with_questions]

    if keep:
        connection.executemany(
            "UPDATE chunks SET archived = 1, updated_at = ? WHERE id = ?",
            [(utcnow(), chunk_id) for chunk_id in keep],
        )
    deleted = connection.execute(
        """DELETE FROM chunks
           WHERE document_id = ? AND archived = 0
             AND id NOT IN (SELECT DISTINCT chunk_id FROM questions)""",
        (document_id,),
    ).rowcount

    remaining = connection.execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE document_id = ?", (document_id,)
    ).fetchone()["n"]
    if not remaining:
        connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    connection.commit()

    return {"archived": len(keep), "deleted": deleted,
            "document_deleted": int(not remaining)}


def unload_preview(connection: sqlite3.Connection, document_id: int) -> dict[str, int]:
    """What `unload_document` would do, without doing it."""
    row = connection.execute(
        """SELECT
             (SELECT COUNT(DISTINCT c.id) FROM chunks c
              JOIN questions q ON q.chunk_id = c.id
              WHERE c.document_id = ? AND c.archived = 0) AS keeping,
             (SELECT COUNT(*) FROM chunks WHERE document_id = ? AND archived = 0)
                AS total,
             (SELECT COUNT(*) FROM questions q JOIN chunks c ON c.id = q.chunk_id
              WHERE c.document_id = ?) AS questions""",
        (document_id, document_id, document_id),
    ).fetchone()
    keeping = int(row["keeping"])
    return {"archived": keeping, "deleted": int(row["total"]) - keeping,
            "questions": int(row["questions"])}


def relocate_document(connection: sqlite3.Connection, document_id: int,
                      new_path: str | Path) -> bool:
    """Points a document at the same PDF in a new place.

    The path is absolute and recorded at import, so it stops being true the
    moment the file moves, the folder is renamed, or the database is opened on
    another machine. Everything else about the document survives that -- the
    pages, the quotes and the highlight rectangles are all offsets into the
    file's contents, not into its location.

    The hash decides whether it really is the same file. A different edition
    would keep every stored page number while the text underneath had shifted,
    which is worse than the broken path: the reveal would land confidently on
    the wrong paragraph.
    """
    from ..ingest.extract import file_sha256

    row = get_document(connection, document_id)
    if row is None:
        return False
    if file_sha256(new_path) != row["sha256"]:
        return False

    # The name follows the file. Leaving it behind meant the picker went on
    # showing a filename that no longer exists on disk -- and, worse, the "Find
    # ..." dialog sent the reader looking for it under the old name, at exactly
    # the moment they were asking the app where the file went.
    resolved = Path(new_path).resolve()
    connection.execute("UPDATE documents SET path = ?, filename = ? WHERE id = ?",
                       (str(resolved), resolved.name, document_id))
    connection.commit()
    return True


def document_is_present(connection: sqlite3.Connection, document_id: int) -> bool:
    row = get_document(connection, document_id)
    return bool(row and Path(row["path"]).exists())


# --- starting over ----------------------------------------------------------


def progress_totals(connection: sqlite3.Connection) -> dict[str, int]:
    """What a reset would erase, for the confirmation to quote back."""
    row = connection.execute(
        """SELECT
             (SELECT COUNT(*) FROM attempts) AS attempts,
             (SELECT COUNT(*) FROM sessions) AS sessions,
             (SELECT COUNT(DISTINCT question_id) FROM attempts) AS questions"""
    ).fetchone()
    return {key: int(row[key]) for key in row.keys()}


def reset_progress(connection: sqlite3.Connection) -> dict[str, int]:
    """Erases the record of practising, and nothing else.

    Everything the bank is made of stays: the questions, their quotes, pages and
    highlight rectangles, the chunks behind them and the document itself. What
    goes is `attempts` and `sessions` -- which is also the whole of the mistakes
    queue and the per-chapter accuracy, since both are derived from attempts
    rather than stored.

    `starred` is deliberately left alone. A star is a note to come back to a
    question, not a record of having answered it, and someone clearing their
    history rarely means to throw those away too.

    This is the one place in the app that truly deletes. It is safe only because
    nothing else refers to these rows.
    """
    totals = progress_totals(connection)
    connection.execute("DELETE FROM attempts")
    connection.execute("DELETE FROM sessions")
    connection.commit()
    return totals
