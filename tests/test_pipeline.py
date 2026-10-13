"""The whole chunk pipeline, with a stand-in for the model.

What is being checked is the wiring: that a reply becomes rows with a page and
rectangles filled in, that the page came from the search rather than anywhere
else, and that the three outcomes a chunk can have -- done, skipped, failed --
are told apart correctly. A chunk with nothing to ask about is `skipped`; making
it `failed` would bury the chunks that really did break.
"""

import json
from pathlib import Path

import pymupdf
import pytest

from studyapp.generate import pipeline

FIXTURES = Path(__file__).parent / "fixtures"


class FakeBackend:
    """Returns a canned reply, or raises. Records what it was asked."""

    name = "fake"

    def __init__(self, questions=None, error=None):
        self.questions = questions or []
        self.error = error
        self.system = None
        self.user = None
        self.costs = type("Costs", (), {"status_text": lambda self: "free"})()

    def generate(self, system, user, is_cancelled=None):
        self.system, self.user = system, user
        if self.error:
            raise self.error
        return self.questions


@pytest.fixture(scope="module")
def chunk_text() -> str:
    return (FIXTURES / "chunk_transakce.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pdf_path(tmp_path_factory, chunk_text) -> str:
    """A PDF holding the fixture text, so quotes can genuinely be located in it."""
    path = tmp_path_factory.mktemp("pipeline") / "chunk.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((72, 300), "Titulní strana",
                                    fontsize=20, fontname="hebo")
    for part in (chunk_text[:len(chunk_text) // 2], chunk_text[len(chunk_text) // 2:]):
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(60, 60, 400, 760), part,
                            fontsize=10, fontname="helv")
    document.save(path)
    document.close()
    return str(path)


@pytest.fixture
def chunk(chunk_text):
    return {"id": 7, "text": chunk_text, "title": "6.2 Vlastnosti transakcí",
            "page_start": 1, "page_end": 2}


@pytest.fixture
def document(pdf_path):
    return {"id": 1, "path": pdf_path, "language": "cs"}


def reply(name: str) -> list[dict]:
    payload = json.loads((FIXTURES / "replies" / f"{name}.json").read_text(encoding="utf-8"))
    return payload["questions"]


class TestSuccessfulChunk:
    def test_questions_come_back_ready_for_the_database(self, chunk, document):
        result = pipeline.generate_for_chunk(FakeBackend(reply("good")), chunk, document)
        assert result.error is None
        assert result.status == "done"
        assert len(result.questions) == 3
        for question in result.questions:
            assert set(question) >= {"type", "question", "source_quote",
                                     "quote_score", "page", "rects"}

    def test_pages_are_found_not_guessed(self, chunk, document):
        result = pipeline.generate_for_chunk(FakeBackend(reply("good")), chunk, document)
        pages = [question["page"] for question in result.questions]
        assert all(page is not None for page in pages), pages
        # Page 0 is the title page; nothing quoted can be on it.
        assert all(page in (1, 2) for page in pages)

    def test_rectangles_are_filled_in(self, chunk, document):
        result = pipeline.generate_for_chunk(FakeBackend(reply("good")), chunk, document)
        assert result.located == 3
        assert all(question["rects"] for question in result.questions)

    def test_the_chapter_title_reaches_the_prompt(self, chunk, document):
        backend = FakeBackend(reply("good"))
        pipeline.generate_for_chunk(backend, chunk, document)
        assert "6.2 Vlastnosti transakcí" in backend.user
        assert chunk["text"] in backend.user

    def test_the_source_language_reaches_the_prompt(self, chunk, document):
        backend = FakeBackend(reply("good"))
        pipeline.generate_for_chunk(backend, chunk, document)
        assert "Czech" in backend.system

    def test_no_page_numbers_are_asked_of_the_model(self, chunk, document):
        backend = FakeBackend(reply("good"))
        pipeline.generate_for_chunk(backend, chunk, document)
        assert "not asked for any" in backend.system


class TestRejectedQuestions:
    def test_invalid_questions_never_reach_the_result(self, chunk, document):
        result = pipeline.generate_for_chunk(FakeBackend(reply("mixed")), chunk, document)
        assert len(result.questions) == 2
        assert len(result.validation.rejected) == 4

    def test_a_fully_rejected_chunk_is_skipped_not_failed(self, chunk, document):
        result = pipeline.generate_for_chunk(
            FakeBackend(reply("translated_quote")), chunk, document)
        assert result.questions == []
        assert result.status == "skipped"
        assert result.error is None


class TestEmptyAndFailed:
    def test_no_questions_is_skipped(self, chunk, document):
        # A contents page or a plate section genuinely warrants none.
        result = pipeline.generate_for_chunk(FakeBackend([]), chunk, document)
        assert result.status == "skipped" and result.error is None

    def test_a_backend_failure_is_reported_not_raised(self, chunk, document):
        result = pipeline.generate_for_chunk(
            FakeBackend(error=RuntimeError("HTTP 529")), chunk, document)
        assert result.status == "failed"
        assert "529" in result.error
        assert result.questions == []

    def test_the_chunk_id_survives_a_failure(self, chunk, document):
        result = pipeline.generate_for_chunk(
            FakeBackend(error=RuntimeError("boom")), chunk, document)
        assert result.chunk_id == 7

    def test_a_missing_pdf_fails_the_chunk_rather_than_the_run(self, chunk):
        result = pipeline.generate_for_chunk(
            FakeBackend(reply("good")), chunk, {"path": "nope.pdf", "language": "cs"})
        assert result.status == "failed"


class TestLocateQuestions:
    def test_an_unfindable_quote_leaves_the_page_empty(self, pdf_path):
        # Better an empty page than an invented one: the question lands in
        # "needs review", which is where it belongs anyway.
        questions = [{"source_quote": "Tahle věta v dokumentu vůbec není."}]
        located = pipeline.locate_questions(questions, pdf_path, 0, 2)
        assert located[0]["page"] is None
        assert located[0]["rects"] is None

    def test_nothing_to_locate(self, pdf_path):
        assert pipeline.locate_questions([], pdf_path, 0, 2) == []
