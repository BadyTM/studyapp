"""Retiring the questions that ask the same thing twice.

Two hundred pages generate somewhere between six hundred and a thousand
questions, chunk by chunk, with no chunk knowing what any other one asked. The
overlap is considerable and it is invisible until you are half way through a
session answering the same thing for the third time.

Run as its own CLI, in its own virtualenv:

    python -m studyapp.embed.dedupe --dry-run
    python -m studyapp.embed.dedupe

The comparison is a single matrix multiply against every other question. At a
thousand questions that is a million dot products and a few milliseconds; a
vector database would be a dependency, a service and a schema to solve a problem
that numpy solves on one line.

The maths below imports nothing heavier than numpy. Only `--encode` reaches for
the model, and only through encode.py.
"""

import argparse
import sys
from dataclasses import dataclass

import numpy as np

from ..db import connection as db_connection
from ..db import repo

# Above this two questions are the same question. Set from reading pairs: 0.95
# misses obvious restatements, 0.85 starts catching genuinely different questions
# about one paragraph.
THRESHOLD = 0.90


@dataclass
class Duplicate:
    """One question judged a restatement of another."""

    keep_id: int
    drop_id: int
    score: float
    keep_text: str
    drop_text: str


def similarity_matrix(vectors: np.ndarray) -> np.ndarray:
    """Cosine similarity between every pair.

    The vectors arrive normalised, so the dot product *is* the cosine and the
    whole thing is one multiply.
    """
    if vectors.size == 0:
        return np.empty((0, 0), dtype=np.float32)
    return vectors @ vectors.T


def find_duplicates(rows, vectors: np.ndarray,
                    threshold: float = THRESHOLD) -> list[Duplicate]:
    """Decides which of each near-identical pair to retire.

    The one with the longer `source_quote` survives. A longer quote gives the
    reveal more to highlight and is the better anchor of the two -- and it is a
    rule, so the outcome does not depend on row order.

    Pairs are walked strongest-first, and a question already retired is not
    considered again, so one survivor is left per cluster of near-identical
    questions.

    Worth knowing about the edge of that: in a chain -- A matches B, B matches C,
    A does not match C -- B wins both pairs and A and C are both retired, even
    though those two are not duplicates of each other. Resolving it properly
    means clustering rather than pairing, and at a threshold of 0.9 the chains
    are rare and short. It is the reason the CLI prints every pair it intends to
    retire and has a --dry-run, rather than the reason to write a clustering
    algorithm.
    """
    if len(rows) < 2:
        return []

    scores = similarity_matrix(vectors)
    # Only the upper triangle: every pair once, and never a question against
    # itself, which would score 1.0 and retire everything.
    candidates = [
        (float(scores[i, j]), i, j)
        for i, j in zip(*np.triu_indices(len(rows), k=1))
        if scores[i, j] >= threshold
    ]
    candidates.sort(reverse=True)

    retired: set[int] = set()
    duplicates: list[Duplicate] = []
    for score, i, j in candidates:
        if i in retired or j in retired:
            continue
        keep, drop = _rank(rows[i], rows[j])
        retired.add(j if drop["id"] == rows[j]["id"] else i)
        duplicates.append(Duplicate(
            keep_id=keep["id"], drop_id=drop["id"], score=score,
            keep_text=keep["question"], drop_text=drop["question"],
        ))
    return duplicates


def _rank(first, second):
    """(keeper, loser) -- longer quote wins, and the lower id breaks a tie."""
    first_length = len(first["source_quote"] or "")
    second_length = len(second["source_quote"] or "")
    if first_length != second_length:
        return (first, second) if first_length > second_length else (second, first)
    return (first, second) if first["id"] < second["id"] else (second, first)


# --- the CLI ----------------------------------------------------------------


def compute_missing_embeddings(connection, model_name: str) -> int:
    """Encodes the questions that have no vector yet. Loads the model."""
    from . import encode

    rows = repo.questions_for_embedding(connection, only_missing=True)
    if not rows:
        return 0

    print(f"Encoding {len(rows)} questions with {model_name} ...")
    vectors = encode.encode([row["question"] for row in rows], name=model_name)
    return repo.store_embeddings(
        connection,
        [(row["id"], encode.to_blob(vector)) for row, vector in zip(rows, vectors)],
    )


def load_embedded(connection):
    """Active questions that have a vector, and the matrix of those vectors."""
    from . import encode

    rows = [row for row in repo.questions_for_embedding(connection)
            if row["embedding"]]
    if not rows:
        return [], np.empty((0, 0), dtype=np.float32)
    return rows, encode.stack([row["embedding"] for row in rows])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m studyapp.embed.dedupe",
        description="Retire questions that duplicate another. Never deletes.")
    parser.add_argument("--threshold", type=float, default=THRESHOLD)
    parser.add_argument("--dry-run", action="store_true",
                        help="list the pairs and change nothing")
    parser.add_argument("--model", default=None,
                        help="sentence-transformer to encode with")
    parser.add_argument("--skip-encode", action="store_true",
                        help="use the stored vectors only; does not load the model")
    parser.add_argument("--limit-shown", type=int, default=20)
    arguments = parser.parse_args(argv)

    db_connection.ensure_schema()
    connection = db_connection.connect()
    try:
        if not arguments.skip_encode:
            from . import encode

            model_name = arguments.model or encode.DEFAULT_MODEL
            try:
                written = compute_missing_embeddings(connection, model_name)
                if written:
                    print(f"Stored {written} embeddings.\n")
            except ImportError:
                print("sentence-transformers is not installed in this environment.\n"
                      "It belongs in a separate virtualenv -- it pulls in PyTorch, "
                      "which must never end up in the app:\n"
                      "    python -m venv .venv-embed\n"
                      "    .venv-embed\\Scripts\\python.exe -m pip install "
                      "sentence-transformers\n"
                      "    .venv-embed\\Scripts\\python.exe -m studyapp.embed.dedupe",
                      file=sys.stderr)
                return 2

        rows, vectors = load_embedded(connection)
        if len(rows) < 2:
            print("Not enough embedded questions to compare.")
            return 0

        duplicates = find_duplicates(rows, vectors, arguments.threshold)
        print(f"{len(rows)} active questions · {len(duplicates)} duplicates "
              f"at similarity >= {arguments.threshold}\n")

        for duplicate in duplicates[:arguments.limit_shown]:
            print(f"  {duplicate.score:.3f}")
            print(f"    keep #{duplicate.keep_id}: {duplicate.keep_text[:88]}")
            print(f"    drop #{duplicate.drop_id}: {duplicate.drop_text[:88]}")
        if len(duplicates) > arguments.limit_shown:
            print(f"  ... and {len(duplicates) - arguments.limit_shown} more")

        if arguments.dry_run:
            print("\nDry run, nothing changed.")
            return 0
        if not duplicates:
            return 0

        retired = repo.set_active(
            connection, [duplicate.drop_id for duplicate in duplicates], False)
        print(f"\nRetired {retired} questions (is_active = 0; nothing was deleted).")
        print(f"{repo.count_questions(connection)} active questions remain.")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
