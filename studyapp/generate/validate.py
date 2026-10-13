"""The gate every generated question passes through before it exists.

This is the only place that decides what gets into the bank, which makes it the
one file worth over-testing. It answers three questions about each question the
model returned:

  * is `source_quote` really in the chunk, or did the model paraphrase it?
  * for multiple choice, does `correct` actually name one of the options?
  * where in the chunk does the quote sit, so the page can be worked out?

A rejection is never silent. Every discarded question comes back with a reason,
because the reasons in aggregate are how the prompt gets fixed -- forty questions
lost to TRANSLATED_QUOTE says something very different from forty lost to
CORRECT_NOT_IN_OPTIONS.
"""

import difflib
import random
import re
from dataclasses import dataclass, field
from enum import Enum

# Below this the quote is too short to locate honestly: a handful of words
# matches in a dozen places and highlights none of them usefully.
MIN_QUOTE_CHARS = 30

# From the plan: a quote the model has tidied up (spacing, an accent, a comma)
# should still pass; one it has rewritten should not.
QUOTE_MATCH_THRESHOLD = 0.9

# The UI offers options on keys 1-4 and the card is laid out for four rows.
# A different count means the model ignored the instruction.
MCQ_OPTION_COUNT = 4


class Rejection(str, Enum):
    MISSING_FIELD = "missing_field"
    UNKNOWN_TYPE = "unknown_type"
    QUOTE_TOO_SHORT = "quote_too_short"
    QUOTE_NOT_FOUND = "quote_not_found"
    WRONG_OPTION_COUNT = "wrong_option_count"
    DUPLICATE_OPTIONS = "duplicate_options"
    CORRECT_NOT_IN_OPTIONS = "correct_not_in_options"
    DUPLICATE_QUESTION = "duplicate_question"
    EMPTY_QUESTION = "empty_question"


@dataclass
class Rejected:
    reason: Rejection
    question: dict
    detail: str = ""


@dataclass
class Result:
    accepted: list[dict] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        total = len(self.accepted) + len(self.rejected)
        return len(self.accepted) / total if total else 0.0

    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.rejected:
            counts[item.reason.value] = counts.get(item.reason.value, 0) + 1
        return counts


# --- locating a quote in the chunk ------------------------------------------


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Whitespace-collapsed text, plus where each character came from.

    The map is what makes it possible to report a match back in terms of the
    original text: the quote is found in collapsed space, but the offsets that
    matter -- for snapping the quote to the source, and later for the page search
    -- are in the text as stored.
    """
    collapsed: list[str] = []
    origin: list[int] = []
    in_space = False
    for index, character in enumerate(text):
        if character.isspace():
            if not in_space and collapsed:
                collapsed.append(" ")
                origin.append(index)
            in_space = True
            continue
        in_space = False
        collapsed.append(character)
        origin.append(index)
    while collapsed and collapsed[-1] == " ":
        collapsed.pop()
        origin.pop()
    return "".join(collapsed), origin


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def fold_with_map(text: str) -> tuple[str, list[int]]:
    """Case-folded text, plus where each folded character came from.

    `str.casefold()` is not length-preserving: `ﬁ` folds to two characters, `ß`
    to `ss`, `İ` to two. A position found in the folded string therefore does not
    index the unfolded one, and every such character before a match shifted the
    stored quote by one -- "Atomicita ..." came out as "tomicita ...", with a
    score of 1.0 and nothing to suggest anything was wrong.

    Ligatures are common in typeset PDFs and `ß` in German, so this is not an
    exotic case; folding one character at a time and recording where each piece
    came from is what keeps the two strings in step.
    """
    folded: list[str] = []
    origin: list[int] = []
    for index, character in enumerate(text):
        pieces = character.casefold()
        folded.append(pieces)
        origin.extend([index] * len(pieces))
    return "".join(folded), origin


@dataclass
class Located:
    """Where a quote sits in the chunk, and how well it matched."""

    score: float
    start: int          # offset into the original chunk text
    end: int
    text: str           # the chunk's own wording, not the model's


def locate_quote(quote: str, chunk_text: str) -> Located | None:
    """Finds a quote in the chunk, exactly if possible and approximately if not.

    On a match the chunk's own wording is returned rather than the model's. That
    is not an edit -- it is the opposite: the stored quote becomes literally a
    slice of the source, which is what the highlighting in the PDF needs it to be.
    """
    quote = _collapse(quote)
    if len(quote) < MIN_QUOTE_CHARS:
        return None

    haystack, origin = normalize_with_map(chunk_text)
    if not haystack:
        return None

    # Searched in folded space, reported back in unfolded space. The map is what
    # makes the second half possible when folding changed the length.
    folded, fold_origin = fold_with_map(haystack)
    folded_quote = quote.casefold()

    # Exact, case-insensitively: the overwhelmingly common case.
    position = folded.find(folded_quote)
    if position >= 0:
        start, length = _unfold(fold_origin, position, len(folded_quote))
        return _slice(haystack, origin, chunk_text, start, length, 1.0)

    best_score, best_position = _best_window(folded, folded_quote)
    if best_score < QUOTE_MATCH_THRESHOLD:
        return None

    # The window is as long as the quote the model sent, which is not the same
    # as the run of source it matched. A quote with a word added slid the window
    # backwards to make up the length, and the stored text began in the previous
    # paragraph: the tail of the sentence before it, then the quote, stored at a
    # score of 0.92. Trimming to where the two actually agree keeps the stored
    # quote inside the sentence it came from.
    span = _agreeing_span(folded, folded_quote, best_position)
    if span is None:
        return None
    folded_start, folded_length = span
    start, length = _unfold(fold_origin, folded_start, folded_length)
    return _slice(haystack, origin, chunk_text, start, length, best_score)


def _agreeing_span(folded: str, needle: str, position: int) -> tuple[int, int] | None:
    """Where the window and the quote really line up, as (start, length).

    `_best_window` reports where to look; this says how much of it to keep.
    `SequenceMatcher`'s matching blocks give the first and last place the two
    strings agree, and everything outside that pair is the window overshooting.
    """
    window = folded[position:position + len(needle)]
    matcher = difflib.SequenceMatcher(None, window, needle, autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size]
    if not blocks:
        return None
    first, last = blocks[0], blocks[-1]
    start = position + first.a
    end = position + last.a + last.size
    return start, end - start


def _unfold(fold_origin: list[int], position: int, length: int) -> tuple[int, int]:
    """Turns a span in folded text back into one in the text it was folded from."""
    if not fold_origin:
        return 0, 0
    first = fold_origin[min(position, len(fold_origin) - 1)]
    last_index = min(position + length, len(fold_origin)) - 1
    last = fold_origin[max(last_index, 0)]
    return first, last - first + 1


def _slice(haystack, origin, chunk_text, position, length, score) -> Located:
    end_index = min(position + length, len(origin)) - 1
    start = _word_start(chunk_text, origin[position])
    end = _word_end(chunk_text, origin[end_index] + 1)
    return Located(score=round(score, 4), start=start, end=end,
                   text=chunk_text[start:end].strip())


def _word_start(text: str, start: int) -> int:
    """Backs up to the beginning of the word the span starts inside.

    An approximate match lines up on characters, not on words, so a span could
    begin after the first letters of a word: `žurnál` stored as `rnál`. The
    quote is still a literal run of the source, which is the rule that matters,
    and it still reads as damage -- in the question, and in the marker drawn
    over the page.
    """
    while start > 0 and text[start - 1].isalnum() and text[start].isalnum():
        start -= 1
    return start


def _word_end(text: str, end: int) -> int:
    """The same at the other end, where a span could stop mid-word."""
    while end < len(text) and text[end].isalnum() and text[end - 1].isalnum():
        end += 1
    return end


def _best_window(haystack: str, needle: str) -> tuple[float, int]:
    """The best-matching window of `haystack` against `needle`.

    A coarse sweep followed by a fine one around the winner. Comparing every
    offset would be exact and far too slow; the two-pass version misses only when
    the true match is worse than a decoy half a step away, which in a 2000-word
    chunk of prose does not happen.
    """
    length = len(needle)
    if length > len(haystack):
        matcher = difflib.SequenceMatcher(None, needle, haystack, autojunk=False)
        return matcher.ratio(), 0

    matcher = difflib.SequenceMatcher(None, autojunk=False)
    matcher.set_seq2(needle)

    def ratio_at(position: int) -> float:
        matcher.set_seq1(haystack[position:position + length])
        # Two cheap upper bounds first; most windows are nowhere near and are
        # thrown out without the real comparison ever running.
        if matcher.real_quick_ratio() < QUOTE_MATCH_THRESHOLD:
            return 0.0
        if matcher.quick_ratio() < QUOTE_MATCH_THRESHOLD:
            return 0.0
        return matcher.ratio()

    coarse_step = max(1, length // 4)
    best_score, best_position = 0.0, 0
    for position in range(0, len(haystack) - length + 1, coarse_step):
        score = ratio_at(position)
        if score > best_score:
            best_score, best_position = score, position

    if best_score == 0.0:
        return 0.0, 0

    low = max(0, best_position - coarse_step)
    high = min(len(haystack) - length, best_position + coarse_step)
    for position in range(low, high + 1):
        score = ratio_at(position)
        if score > best_score:
            best_score, best_position = score, position

    return best_score, best_position


# --- validating one question ------------------------------------------------


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def validate_question(raw: dict, chunk_text: str, shuffle: bool = True,
                      rng: random.Random | None = None) -> dict | Rejected:
    """One question in, either a row ready for the database or a reason it is not."""
    kind = _text(raw.get("type")).lower()
    if kind not in ("mcq", "open"):
        return Rejected(Rejection.UNKNOWN_TYPE, raw, kind)

    question_text = _text(raw.get("question"))
    if not question_text:
        return Rejected(Rejection.EMPTY_QUESTION, raw)

    quote = _text(raw.get("source_quote"))
    correct = _text(raw.get("correct"))
    if not quote or not correct:
        return Rejected(Rejection.MISSING_FIELD, raw,
                        "source_quote" if not quote else "correct")

    if len(_collapse(quote)) < MIN_QUOTE_CHARS:
        return Rejected(Rejection.QUOTE_TOO_SHORT, raw, f"{len(quote)} chars")

    located = locate_quote(quote, chunk_text)
    if located is None:
        # Nearly always one of two things: the model paraphrased, or it translated
        # the quote into the question's language. Both mean the question is not
        # anchored to the text and cannot be trusted.
        return Rejected(Rejection.QUOTE_NOT_FOUND, raw, quote[:60])

    row = {
        "type": kind,
        "question": question_text,
        "explanation": _text(raw.get("explanation")) or None,
        "source_quote": located.text,
        "quote_score": located.score,
        "options": None,
        "correct_index": None,
        "correct_text": None,
    }

    if kind == "open":
        row["correct_text"] = correct
        return row

    options = [_text(option) for option in raw.get("options") or []]
    options = [option for option in options if option]
    if len(options) != MCQ_OPTION_COUNT:
        return Rejected(Rejection.WRONG_OPTION_COUNT, raw, str(len(options)))
    if len({option.casefold() for option in options}) != len(options):
        return Rejected(Rejection.DUPLICATE_OPTIONS, raw)

    # The model rephrases its own answer often enough that matching on the string
    # would quietly mark correct answers wrong. An index cannot drift.
    matches = [index for index, option in enumerate(options)
               if _collapse(option).casefold() == _collapse(correct).casefold()]
    if not matches:
        return Rejected(Rejection.CORRECT_NOT_IN_OPTIONS, raw, correct[:60])

    correct_index = matches[0]
    if shuffle:
        # The model puts the right answer in much the same place every time --
        # usually last, or the longest option -- and a reader picks that up
        # without noticing. Shuffling breaks the tell.
        order = list(range(len(options)))
        (rng or random).shuffle(order)
        options = [options[index] for index in order]
        correct_index = order.index(correct_index)

    row["options"] = options
    row["correct_index"] = correct_index
    return row


def validate_questions(raw_questions, chunk_text: str, shuffle: bool = True,
                       rng: random.Random | None = None) -> Result:
    """Validates a whole reply, dropping questions that repeat each other."""
    result = Result()
    seen: set[str] = set()

    for raw in raw_questions or []:
        outcome = validate_question(raw, chunk_text, shuffle=shuffle, rng=rng)
        if isinstance(outcome, Rejected):
            result.rejected.append(outcome)
            continue

        # Within one chunk the model sometimes asks the same thing twice in
        # different words; identical wording is at least the free half of that.
        fingerprint = _collapse(outcome["question"]).casefold()
        if fingerprint in seen:
            result.rejected.append(Rejected(Rejection.DUPLICATE_QUESTION, raw))
            continue
        seen.add(fingerprint)
        result.accepted.append(outcome)

    return result
