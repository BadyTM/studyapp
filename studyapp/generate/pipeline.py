"""One chunk in, database-ready questions out.

Deliberately a plain function with no Qt and no database in sight. Phase 4 runs
it on a thread pool and hands the result back by signal; the pilot CLI runs it in
a loop. Neither can write to SQLite from here, which is the point -- several
threads writing to one SQLite file is how `database is locked` happens, and it
happens non-deterministically.

The API call and the rectangle search are one step rather than two because they
answer the same question. The model returns a quote; where that quote turns out
to sit gives the page and the highlight together, and a page arrived at this way
cannot be invented.
"""

from dataclasses import dataclass, field

import pymupdf

from .. import config
from ..ingest import language as language_module
from . import prompts, rects, validate


@dataclass
class ChunkResult:
    """Everything the writing thread needs to record one chunk's outcome."""

    chunk_id: int
    questions: list[dict] = field(default_factory=list)
    validation: validate.Result | None = None
    error: str | None = None
    # A chunk the run never reached, because it was stopped first. Reported
    # rather than dropped: the controller counts every chunk it queued, and a
    # job that returns in silence leaves the run believing it is still going.
    cancelled: bool = False

    @property
    def status(self) -> str:
        """What to store in `chunks.status`.

        A chunk with no questions is `skipped`, not `failed`. A contents page or
        a plate section genuinely has nothing to ask about, and counting that as
        a failure hides the chunks that really did break.

        A cancelled chunk goes back to `pending`: it was marked `running` before
        its job started, and leaving it that way would show a run still in
        progress that nothing is working on.
        """
        if self.cancelled:
            return "pending"
        if self.error:
            return "failed"
        return "done" if self.questions else "skipped"

    @property
    def located(self) -> int:
        return sum(1 for question in self.questions if question.get("page") is not None)


def generate_for_chunk(backend, chunk, document, is_cancelled=None) -> ChunkResult:
    """Generates, validates and locates the questions for a single chunk.

    `chunk` and `document` are rows (or anything with the same fields). Nothing
    here touches the database; the caller decides what to do with the result.
    """
    chunk_id = chunk["id"]
    try:
        source_language = language_module.name(document["language"])
        question_language = (
            source_language if config.QUESTION_LANGUAGE == "source"
            else config.QUESTION_LANGUAGE
        )

        raw = backend.generate(
            prompts.system_prompt(
                source_language=source_language,
                question_language=question_language,
                min_questions=config.MIN_QUESTIONS_PER_CHUNK,
                max_questions=config.MAX_QUESTIONS_PER_CHUNK,
                output_instruction=getattr(backend, "output_instruction",
                                           prompts.TOOL_OUTPUT),
            ),
            prompts.user_message(chunk["text"], chunk["title"]),
            is_cancelled=is_cancelled,
        )

        result = validate.validate_questions(raw, chunk["text"])
        questions = locate_questions(result.accepted, document["path"],
                                     chunk["page_start"], chunk["page_end"])
        return ChunkResult(chunk_id=chunk_id, questions=questions, validation=result)

    except Exception as error:          # noqa: BLE001 - reported, not swallowed
        from .client import describe_error

        return ChunkResult(chunk_id=chunk_id, error=describe_error(error))


def locate_questions(questions: list[dict], pdf_path: str,
                     page_start: int, page_end: int) -> list[dict]:
    """Fills in `page` and `rects` for a chunk's questions.

    The document is opened once for the whole chunk. Opening a 200-page PDF per
    question turns a generation run from minutes into an afternoon.
    """
    if not questions:
        return []

    with pymupdf.open(pdf_path) as document:
        for question in questions:
            page, boxes = rects.locate(document, page_start, page_end,
                                       question["source_quote"])
            question["page"] = page
            question["rects"] = boxes or None
    return questions
