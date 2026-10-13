"""Turning question text into vectors.

`sentence-transformers` is imported inside the functions, never at module level.
That is not tidiness -- it is the one thing keeping PyTorch, some two gigabytes
of it, out of the application and out of the eventual build. Everything in this
package runs from a separate CLI in its own virtualenv, and the app must be able
to import `studyapp.db.repo` all day without any of it appearing.

The vector maths lives in dedupe.py and needs none of this, which is what makes
the deduplication logic testable without a model on disk.
"""

import numpy as np

# Both handle Czech well and run on CPU. e5-base is a third of the size of bge-m3
# and quick enough on a thousand questions; bge-m3 is the better model if the
# bank ever gets big enough for that to matter.
DEFAULT_MODEL = "intfloat/multilingual-e5-base"

# e5 models are trained with these prefixes and lose accuracy without them.
QUERY_PREFIX = "query: "

BATCH_SIZE = 32


def load_model(name: str = DEFAULT_MODEL):
    """Loads the sentence-transformer. Slow and heavy; call once."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(name, device="cpu")


def encode(texts: list[str], model=None, name: str = DEFAULT_MODEL,
           on_progress=None) -> np.ndarray:
    """Encodes texts into unit-length float32 vectors.

    Normalised on the way out so that cosine similarity is a plain dot product --
    which is what lets the whole comparison be one matrix multiply.
    """
    model = model or load_model(name)
    prefixed = [QUERY_PREFIX + text for text in texts]
    vectors = model.encode(
        prefixed,
        batch_size=BATCH_SIZE,
        normalize_embeddings=True,
        show_progress_bar=on_progress is None,
        convert_to_numpy=True,
    )
    return vectors.astype(np.float32)


def to_blob(vector: np.ndarray) -> bytes:
    """The form stored in `questions.embedding`."""
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def stack(blobs: list[bytes]) -> np.ndarray:
    """Rebuilds a matrix from stored blobs, one row per question."""
    return np.vstack([from_blob(blob) for blob in blobs]) if blobs else np.empty((0, 0))
