"""Finds where a quote physically sits in the PDF.

One pass answers both open questions at once: which page the quote is on, and
which rectangles to paint the marker over. That is the whole reason the page
number is never asked of the model -- it falls out of a search that has to happen
anyway, and a searched page number cannot be invented.

Runs once, at generation time. Nothing here is imported by the running app; the
quiz reads finished rectangles out of the database.
"""

import difflib
import re

import pymupdf

# A short prefix is more likely to sit on one line than the whole sentence, and
# PyMuPDF's own search does not cross a line break.
PREFIX_CHARS = 80
ANCHOR_WORDS = 6

# The word-by-word fallback is matching against text pulled out of the same PDF,
# so a near-miss here means hyphenation or a ligature, not a different sentence.
WORD_MATCH_THRESHOLD = 0.85

# Good enough that no later page can be a better answer, so the search stops.
CERTAIN = 0.99

# Words on the same line share a baseline to within a point or two.
LINE_TOLERANCE = 3.0

# How far apart two boxes on one baseline may be and still be the same line.
# A space is a few points; the gutter between two columns is tens of them.
COLUMN_GAP = 24.0


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def merge_line_rects(boxes: list[tuple[float, float, float, float]]) -> list[list[float]]:
    """Joins per-word boxes into one rectangle per line of text.

    A marker drawn as thirty little boxes has visible gaps between the words and
    reads as thirty highlights; drawn per line it reads as one.
    """
    if not boxes:
        return []
    ordered = sorted(boxes, key=lambda box: (round(box[1], 1), box[0]))
    lines: list[list[float]] = []
    current = list(ordered[0])
    for box in ordered[1:]:
        # Same baseline is not enough on its own. Two columns share every
        # baseline on the page, so a word in each merged into one band straight
        # through the gutter -- a highlight over text that was never quoted.
        # Words on one line follow each other closely; a column gap does not.
        gap = box[0] - current[2]
        if abs(box[1] - current[1]) <= LINE_TOLERANCE and gap <= COLUMN_GAP:
            current[0] = min(current[0], box[0])
            current[1] = min(current[1], box[1])
            current[2] = max(current[2], box[2])
            current[3] = max(current[3], box[3])
        else:
            lines.append([round(value, 2) for value in current])
            current = list(box)
    lines.append([round(value, 2) for value in current])
    return lines


def match_over_words(words: list[tuple], quote: str) -> tuple[float, list[list[float]]]:
    """Locates a quote across word boxes, tolerating line breaks and hyphens.

    `page.search_for` gives up when a sentence wraps or a word is split across a
    line, which in a justified textbook is most long sentences. Matching over the
    word list has neither problem, because the line structure is gone by then.

    Returns `(score, rectangles)` -- the score so that `locate` can compare one
    page against another instead of taking the first that answers at all.
    """
    if not words:
        return 0.0, []
    tokens = [entry[4] for entry in words]
    target = _collapse(quote)
    target_length = len(target.split())
    if target_length == 0:
        return 0.0, []

    matcher = difflib.SequenceMatcher(None, autojunk=False)
    matcher.set_seq2(target.casefold())

    best_score, best_span = 0.0, None
    # A hyphenated break adds a token, a ligature can lose one, so the window is
    # allowed to breathe a little either side of the expected word count.
    for width in {target_length, target_length + 1, target_length + 2, max(1, target_length - 1)}:
        for start in range(0, len(tokens) - width + 1):
            candidate = " ".join(tokens[start:start + width])
            matcher.set_seq1(candidate.casefold())
            if matcher.real_quick_ratio() < WORD_MATCH_THRESHOLD:
                continue
            if matcher.quick_ratio() < WORD_MATCH_THRESHOLD:
                continue
            score = matcher.ratio()
            if score > best_score:
                best_score, best_span = score, (start, start + width)

    if best_span is None or best_score < WORD_MATCH_THRESHOLD:
        return 0.0, []
    start, end = best_span
    return best_score, merge_line_rects(
        [tuple(entry[:4]) for entry in words[start:end]])


def locate_on_page(page: "pymupdf.Page", quote: str) -> tuple[float, list[list[float]]]:
    """How well a quote sits on this page, and the rectangles covering it.

    The word match comes first and the prefix search is the fallback, which is
    the opposite of how this started. `search_for(quote[:80])` is the quickest
    way to find out whether a page holds the quote, and it was also what painted
    the marker -- so the marker covered the first eighty characters of a
    two-hundred-character quote and stopped, mid-word. Measured over a real bank,
    176 questions out of 180 were highlighted that way.

    The score is what lets `locate` compare pages. An exact word match scores
    near 1.0; the prefix fallbacks score low on purpose, because finding the
    opening of a sentence is much weaker evidence than matching all of it.
    """
    quote = _collapse(quote)
    if not quote:
        return 0.0, []

    score, boxes = match_over_words(page.get_text("words"), quote)
    if boxes:
        return score, boxes

    # Nothing matched across the words. The sentence may still open here -- a
    # quote spanning a page break does exactly that -- so report it, weakly.
    found = page.search_for(quote[:PREFIX_CHARS])
    if found:
        return 0.5, merge_line_rects([tuple(rect) for rect in found])
    found = page.search_for(" ".join(quote.split()[:ANCHOR_WORDS]))
    if found:
        return 0.2, merge_line_rects([tuple(rect) for rect in found])
    return 0.0, []


def locate(
    document: "pymupdf.Document",
    page_start: int,
    page_end: int,
    quote: str,
) -> tuple[int | None, list[list[float]]]:
    """The page a quote is on and the rectangles covering it.

    Every page in the chunk is scored and the best one wins. Taking the first
    page that answered at all was how a six-word opening on an earlier page --
    a chapter recap, say, repeating the sentence's first words -- stole the page
    number from the page that actually held the quote, and got a six-word
    highlight to go with it.

    Only the chunk's own pages are searched -- the quote came from there, and
    widening the search would trade a missing highlight for a confidently wrong
    one somewhere else in the book.

    Returns `(None, [])` when the quote cannot be found. That is a useful answer:
    a quote that is not on the page it came from usually means the model
    paraphrased, so the question is worth a second look regardless of the marker.
    """
    best_score, best_page, best_rects = 0.0, None, []
    for number in range(page_start, min(page_end, document.page_count - 1) + 1):
        score, rects = locate_on_page(document[number], quote)
        if score > best_score:
            best_score, best_page, best_rects = score, number, rects
        # A full match leaves nothing for a later page to beat.
        if best_score >= CERTAIN:
            break

    if best_page is None:
        return None, []
    return best_page, best_rects


def locate_all(path, chunk_page_start: int, chunk_page_end: int,
               quotes: list[str]) -> list[tuple[int | None, list[list[float]]]]:
    """Locates several quotes in one open of the document.

    Opening a 200-page PDF per question is the difference between a generation
    run that takes minutes and one that takes an afternoon.
    """
    with pymupdf.open(path) as document:
        return [locate(document, chunk_page_start, chunk_page_end, quote)
                for quote in quotes]
