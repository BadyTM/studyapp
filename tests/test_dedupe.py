"""Deduplication.

Tested with hand-made vectors rather than a real model. That is deliberate: the
part that can be wrong is which of a pair gets retired and whether the pairing is
transitive, and neither of those needs 2 GB of PyTorch to check. The first test
below asserts the separation itself -- that importing any of this brings in no
torch at all, which is the rule the whole packaging story rests on.
"""

import sys

import numpy as np
import pytest

from studyapp.db import connection as db_connection
from studyapp.db import repo
from studyapp.embed import dedupe


def unit(*values) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


class Row(dict):
    """A stand-in for a sqlite3.Row, which indexes by name the same way."""


def row(question_id: int, question: str, quote: str) -> Row:
    return Row(id=question_id, question=question, source_quote=quote)


class TestNoTorchAnywhere:
    def test_importing_dedupe_does_not_import_torch(self):
        # The rule the packaged build depends on: sentence-transformers, and so
        # PyTorch, is reached only from inside a function.
        assert "torch" not in sys.modules
        assert "sentence_transformers" not in sys.modules

    def test_the_app_does_not_import_the_embed_package(self):
        import studyapp.ui.main_window  # noqa: F401

        assert "studyapp.embed.encode" not in sys.modules
        assert "torch" not in sys.modules


class TestSimilarity:
    def test_identical_vectors_score_one(self):
        vectors = np.vstack([unit(1, 0, 0), unit(1, 0, 0)])
        assert dedupe.similarity_matrix(vectors)[0, 1] == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self):
        vectors = np.vstack([unit(1, 0, 0), unit(0, 1, 0)])
        assert dedupe.similarity_matrix(vectors)[0, 1] == pytest.approx(0.0)

    def test_no_vectors_is_not_a_crash(self):
        assert dedupe.similarity_matrix(np.empty((0, 0))).size == 0


class TestFindingDuplicates:
    def test_a_near_identical_pair_is_found(self):
        rows = [row(1, "Co je transakce?", "kratka citace"),
                row(2, "Co to je transakce?", "podstatne delsi citace ze zdroje")]
        vectors = np.vstack([unit(1, 0, 0), unit(0.99, 0.14, 0)])
        found = dedupe.find_duplicates(rows, vectors, threshold=0.9)
        assert len(found) == 1

    def test_the_longer_quote_survives(self):
        # A longer quote gives the reveal more to highlight and anchors the
        # question better -- and being a rule, the outcome does not depend on
        # which row happened to come first.
        rows = [row(1, "Co je transakce?", "kratka"),
                row(2, "Co to je transakce?", "podstatne delsi citace ze zdroje")]
        vectors = np.vstack([unit(1, 0, 0), unit(1, 0, 0)])
        found = dedupe.find_duplicates(rows, vectors, threshold=0.9)
        assert found[0].keep_id == 2
        assert found[0].drop_id == 1

    def test_row_order_does_not_change_the_outcome(self):
        first = [row(1, "A?", "kratka"), row(2, "B?", "mnohem delsi citace zde")]
        vectors = np.vstack([unit(1, 0, 0), unit(1, 0, 0)])
        forward = dedupe.find_duplicates(first, vectors, threshold=0.9)
        backward = dedupe.find_duplicates(list(reversed(first)), vectors, threshold=0.9)
        assert forward[0].keep_id == backward[0].keep_id == 2

    def test_an_equal_length_tie_goes_to_the_lower_id(self):
        rows = [row(7, "A?", "stejne"), row(3, "B?", "stejne")]
        vectors = np.vstack([unit(1, 0, 0), unit(1, 0, 0)])
        assert dedupe.find_duplicates(rows, vectors, threshold=0.9)[0].keep_id == 3

    def test_distinct_questions_are_left_alone(self):
        rows = [row(1, "Co je transakce?", "citace jedna"),
                row(2, "Co je index?", "citace dva")]
        vectors = np.vstack([unit(1, 0, 0), unit(0, 1, 0)])
        assert dedupe.find_duplicates(rows, vectors, threshold=0.9) == []

    def test_a_question_is_never_a_duplicate_of_itself(self):
        # The diagonal of the matrix is all 1.0; counting it would retire the lot.
        rows = [row(1, "Co je transakce?", "citace")]
        assert dedupe.find_duplicates(rows, np.vstack([unit(1, 0, 0)]), 0.9) == []

    def test_three_of_a_kind_leaves_exactly_one_standing(self):
        rows = [row(1, "A?", "kratka"), row(2, "B?", "stredni citace"),
                row(3, "C?", "nejdelsi citace ze vsech tri")]
        same = unit(1, 0, 0)
        vectors = np.vstack([same, same, same])
        found = dedupe.find_duplicates(rows, vectors, threshold=0.9)
        dropped = {duplicate.drop_id for duplicate in found}
        assert dropped == {1, 2}, "the longest quote survives, the other two go"

    def test_a_chain_collapses_to_its_middle(self):
        # A matches B, B matches C, A does not match C. B wins both pairs, so A
        # and C are both retired despite not being duplicates of one another.
        # Documented rather than fixed: proper clustering for a case that is rare
        # at this threshold, and the CLI shows every pair before acting.
        rows = [row(1, "A?", "citace a"), row(2, "B?", "citace b delsi"),
                row(3, "C?", "citace c")]
        vectors = np.vstack([unit(1, 0, 0.0), unit(1, 0.30, 0), unit(1, 0.75, 0)])
        assert dedupe.similarity_matrix(vectors)[0, 2] < 0.9, "A and C really differ"

        found = dedupe.find_duplicates(rows, vectors, threshold=0.9)
        assert {duplicate.drop_id for duplicate in found} == {1, 3}
        assert {duplicate.keep_id for duplicate in found} == {2}

    def test_the_threshold_is_respected(self):
        rows = [row(1, "A?", "citace a"), row(2, "B?", "citace b")]
        vectors = np.vstack([unit(1, 0, 0), unit(1, 0.35, 0)])   # ~0.94
        assert dedupe.find_duplicates(rows, vectors, 0.9)
        assert dedupe.find_duplicates(rows, vectors, 0.99) == []

    def test_one_question_alone(self):
        assert dedupe.find_duplicates([row(1, "A?", "x")], np.vstack([unit(1, 0)]), 0.9) == []

    def test_nothing_at_all(self):
        assert dedupe.find_duplicates([], np.empty((0, 0)), 0.9) == []


class TestStorage:
    @pytest.fixture
    def database(self, tmp_path):
        path = tmp_path / "dedupe.db"
        db_connection.ensure_schema(path)
        connection = db_connection.connect(path)
        document_id = repo.insert_document(connection, "b.pdf", "b.pdf", "sha", 10, "cs")
        connection.execute(
            """INSERT INTO chunks (document_id, ordinal, page_start, page_end, text,
                                   sha256, status, updated_at)
               VALUES (?, 0, 0, 4, 't', 's', 'done', ?)""",
            (document_id, repo.utcnow()))
        connection.commit()
        chunk_id = connection.execute("SELECT id FROM chunks").fetchone()["id"]
        repo.insert_questions(connection, chunk_id, [
            {"type": "open", "question": f"Otazka {n}?", "correct_text": "a",
             "source_quote": "citace dost dlouha na ulozeni", "quote_score": 1.0,
             "page": 1}
            for n in range(3)
        ])
        yield connection
        connection.close()

    def test_embeddings_round_trip_through_the_blob(self, database):
        from studyapp.embed import encode

        vector = unit(0.1, 0.2, 0.3)
        question_id = database.execute("SELECT MIN(id) AS id FROM questions").fetchone()["id"]
        repo.store_embeddings(database, [(question_id, encode.to_blob(vector))])

        stored = database.execute("SELECT embedding FROM questions WHERE id = ?",
                                  (question_id,)).fetchone()["embedding"]
        assert np.allclose(encode.from_blob(stored), vector)

    def test_questions_without_embeddings_can_be_asked_for(self, database):
        assert len(repo.questions_for_embedding(database, only_missing=True)) == 3

    def test_duplicates_are_retired_not_deleted(self, database):
        # attempts hang off these rows; deleting would lose history or break the
        # foreign key.
        ids = [row["id"] for row in repo.questions_for_embedding(database)][:2]
        repo.deactivate_questions(database, ids)

        assert repo.count_questions(database, active_only=True) == 1
        assert repo.count_questions(database, active_only=False) == 3

    def test_retiring_can_be_undone(self, database):
        ids = [row["id"] for row in repo.questions_for_embedding(database)][:2]
        repo.deactivate_questions(database, ids)
        repo.reactivate_questions(database, ids)
        assert repo.count_questions(database, active_only=True) == 3

    def test_retiring_nothing_is_harmless(self, database):
        assert repo.deactivate_questions(database, []) == 0

    def test_a_retired_question_is_never_practised(self, database):
        ids = [row["id"] for row in repo.questions_for_embedding(database)]
        repo.deactivate_questions(database, ids[:2])
        assert len(repo.pick_questions(database, limit=10)) == 1
