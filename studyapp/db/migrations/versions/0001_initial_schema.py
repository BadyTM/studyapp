"""Initial schema: documents, chunks, questions, sessions, attempts.

The whole schema, in one migration. There is no history worth preserving yet --
the app has only ever run on one machine -- so `chunks.archived` was folded in
here rather than left as a second revision.

`chunks.archived` is what lets a document be taken out of the Generate tab
without losing the questions made from it: a chunk that produced questions is
archived rather than deleted, because those questions reach their chapter, page,
highlight rectangles and the PDF itself through it.

The `srs` table belongs to phase 9 and gets its own migration when spaced
repetition is actually built.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE documents (
            id           INTEGER PRIMARY KEY,
            path         TEXT NOT NULL,
            filename     TEXT NOT NULL,
            sha256       TEXT NOT NULL UNIQUE,
            page_count   INTEGER NOT NULL,
            language     TEXT,              -- source language, questions follow it
            created_at   TEXT NOT NULL      -- ISO 8601 UTC; SQLite has no datetime
        )
    """)

    op.execute("""
        CREATE TABLE chunks (
            id           INTEGER PRIMARY KEY,
            document_id  INTEGER NOT NULL REFERENCES documents(id),
            ordinal      INTEGER NOT NULL,
            page_start   INTEGER NOT NULL,  -- 0-based, matching PyMuPDF and QtPdf
            page_end     INTEGER NOT NULL,
            title        TEXT,              -- chapter heading this chunk sits under
            text         TEXT NOT NULL,
            sha256       TEXT NOT NULL,     -- changed text -> regenerate this chunk
            status       TEXT NOT NULL,     -- pending|running|done|skipped|failed
            error        TEXT,
            archived     INTEGER NOT NULL DEFAULT 0,  -- 0 = still generatable
            updated_at   TEXT NOT NULL
        )
    """)

    op.execute("""
        CREATE TABLE questions (
            id            INTEGER PRIMARY KEY,
            chunk_id      INTEGER NOT NULL REFERENCES chunks(id),
            type          TEXT NOT NULL,     -- mcq | open
            question      TEXT NOT NULL,
            options_json  TEXT,              -- JSON array of options, mcq only
            correct_index INTEGER,           -- index into options_json, mcq only
            correct_text  TEXT,              -- model answer, open only
            explanation   TEXT,
            source_quote  TEXT NOT NULL,     -- verbatim source text, never edited
            quote_score   REAL,              -- quote match against chunk text, 0-1
            page          INTEGER,           -- COMPUTED from the quote, not the model
            rects_json    TEXT,              -- pre-computed highlight rectangles
            embedding     BLOB,              -- np.float32.tobytes()
            is_active     INTEGER NOT NULL DEFAULT 1,   -- 0 = duplicate or retired
            starred       INTEGER NOT NULL DEFAULT 0,
            edited_at     TEXT,              -- hand-edited; regeneration leaves it be
            created_at    TEXT NOT NULL
        )
    """)

    op.execute("""
        CREATE TABLE sessions (
            id           INTEGER PRIMARY KEY,
            started_at   TEXT NOT NULL,
            ended_at     TEXT,
            types_json   TEXT NOT NULL,      -- ["mcq"] | ["open"] | ["mcq","open"]
            source       TEXT NOT NULL,      -- all | mistakes | chapter
            filter_json  TEXT                -- chapter range, question limit
        )
    """)

    op.execute("""
        CREATE TABLE attempts (
            id           INTEGER PRIMARY KEY,
            session_id   INTEGER REFERENCES sessions(id),
            question_id  INTEGER NOT NULL REFERENCES questions(id),
            answered_at  TEXT NOT NULL,
            user_answer  TEXT,               -- chosen option index for mcq, NULL for open
            is_correct   INTEGER NOT NULL,   -- 0/1; self-graded for open questions
            self_graded  INTEGER NOT NULL DEFAULT 0,
            revealed_ms  INTEGER             -- how long before the source was revealed
        )
    """)

    op.execute("CREATE INDEX idx_chunks_status ON chunks(status)")
    # The pending-chunk lookup filters on archived as well as status, so the
    # index on status alone would stop covering it.
    op.execute("CREATE INDEX idx_chunks_archived ON chunks(document_id, archived)")
    op.execute("CREATE INDEX idx_chunks_document ON chunks(document_id, ordinal)")
    op.execute("CREATE INDEX idx_questions_chunk ON questions(chunk_id)")
    op.execute("CREATE INDEX idx_questions_active ON questions(is_active, type)")
    # Supports the mistakes-queue lookup of the newest attempt per question.
    op.execute("CREATE INDEX idx_attempts_question ON attempts(question_id, id)")
    # And this one covers the per-chapter accuracy on the Progress tab, which
    # sums is_correct per question. Without it that sum is an index lookup plus
    # a row read for every attempt there is: measured on 20,000 questions and
    # 60,000 attempts, 122ms against 34ms. Widening the index above to carry
    # is_correct instead was tried and is worse for this query (54ms) -- the
    # scan reads the id column it has no use for.
    op.execute("CREATE INDEX idx_attempts_correct ON attempts(question_id, is_correct)")


def downgrade() -> None:
    for table in ("attempts", "sessions", "questions", "chunks", "documents"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
