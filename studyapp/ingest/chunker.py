"""Merges pages into chunks of roughly one chapter section each.

A chunk is the unit everything downstream works on: it is what one API call sees,
what the generation status is tracked per, and what bounds the search for a
quote's page number. Chunks therefore never straddle more pages than they must,
and they break on chapter headings wherever the size budget allows.
"""

import hashlib
import re
import statistics
from dataclasses import dataclass

from . import extract
from .extract import PageText

# "1.", "1.2", "8.3.1 " -- a numbered heading, optionally followed by its title.
NUMBERED_HEADING = re.compile(r"^\d+(\.\d+){0,3}\.?(\s+\S.*)?$")

# Multi-level numbering -- "1.2", "8.3.1". Distinctive enough to be a heading on
# its own; a bare leading number is not, because running text starts with one
# often enough ("5 lumbar, 5 sacral and 1 coccygeal...") and a numbered list item
# ("1. Mechanotransduction") looks identical to a heading until you check the font.
MULTILEVEL_NUMBER = re.compile(r"^\d+(\.\d+){1,3}\.?(\s+\S.*)?$")

# A sentence ending mid-line and a new one starting: prose, whatever else it
# looks like. Headings do not contain two sentences.
#
# Two things this has to get right, and the character class got both wrong.
# `[A-ZÁ-Ž]` is U+0041-005A plus U+00C1-017D, and that second range is half
# lowercase -- `á`, `č`, `ř`, `ě` all sit inside it -- so "...operaci. ale" read
# as a sentence break. And the dot in a numbered heading is not a full stop:
# "1. Úvod do databází" is the standard Czech heading form, and it was being
# thrown out as prose, which meant such a heading never broke a chunk and never
# gave one its title.
_NUMBER_PREFIX = re.compile(r"^\d+(?:\.\d+)*\.?\s+")
_SENTENCE_GAP = re.compile(r"[.!?]\s+(\S)")


def has_prose_break(text: str) -> bool:
    """Whether the line holds the end of one sentence and the start of another."""
    without_number = _NUMBER_PREFIX.sub("", text, count=1)
    return any(match.group(1).isupper()
               for match in _SENTENCE_GAP.finditer(without_number))

# A heading is short. Anything longer is a paragraph that happens to start big.
MAX_HEADING_WORDS = 12
MAX_HEADING_CHARS = 90

# How much larger than the body text a line must be to count as a heading on size
# alone. Chapter titles in a textbook are typically 1.3-2x the body.
HEADING_SIZE_RATIO = 1.15

# A heading set across more lines than this is not a heading any more.
MAX_HEADING_LINES = 3


@dataclass
class Chunk:
    ordinal: int
    page_start: int      # 0-based, inclusive
    page_end: int        # 0-based, inclusive
    text: str
    title: str | None    # nearest chapter heading at or before page_start
    sha256: str

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def body_size(pages: list[PageText]) -> float:
    """The font size of ordinary running text, as the median over all lines.

    Taken over the whole document rather than per page: a page that is nothing but
    a heading and a figure caption has no body text to compare against.
    """
    sizes = [line.size for page in pages for line in page.lines if line.size > 0]
    return statistics.median(sizes) if sizes else 0.0


def is_heading(line: "extract.Line", body: float) -> bool:
    """Whether a line reads as a chapter or section heading.

    Typography is the strong signal and the others are corroboration. A line set
    larger than the body text, or in bold, is a heading; failing that, only
    multi-level numbering or full capitals will do.

    A bare leading number used to be enough on its own, and on a real document
    that turned "5 lumbar, 5 sacral and 1 coccygeal. They are formed by a" -- an
    ordinary sentence wrapped onto its own line -- into a chapter title, breaking
    the chunk mid-sentence and naming it after the fragment. A numbered list item
    is indistinguishable from a numbered heading by text alone, so the font
    decides.

    Every check is gated on the line being short, which running text never is.
    """
    text = line.text.strip()
    if not text or len(text) > MAX_HEADING_CHARS:
        return False
    words = text.split()
    if len(words) > MAX_HEADING_WORDS:
        return False
    if has_prose_break(text):
        return False        # two sentences on one line
    if text.endswith((".", ",", ";", ":")) and not NUMBERED_HEADING.match(text):
        return False        # a sentence, not a title

    # Set apart from the body: larger, or bold at the same size.
    if body and line.size >= body * HEADING_SIZE_RATIO:
        return True
    if line.bold and body and line.size >= body:
        return True

    # No typographic evidence, so the text has to carry it alone.
    if MULTILEVEL_NUMBER.match(text):
        return True
    letters = [character for character in text if character.isalpha()]
    if len(letters) >= 4 and all(character.isupper() for character in letters):
        return True
    return False


def continues_heading(heading: "extract.Line", following: "extract.Line",
                      body: float) -> bool:
    """Whether `following` is the rest of a heading that wrapped onto two lines.

    Nothing in the PDF marks the second line as a continuation -- it arrives as
    a line like any other, and taking only the first one cut two chapters of a
    real book in half: "Recording of neuronal and glial activity -- extracellular
    and", and "The control of movement -- the role of the basal ganglia", which
    lost "and the cerebellum".

    Joining whenever one heading follows another would be worse than the cut.
    The first page of that same book carries the document's title and its first
    chapter heading in the same 22pt bold, one directly under the other, and
    they are two different things; merged, every question from that chunk would
    be filed under a chapter that does not exist.

    So the rule is the one thing a wrapped line does that a new heading does
    not: it starts in the middle of a sentence, lowercase. The typography has to
    match as well, and the continuation must read as a heading in its own right
    -- that last check is what stops an ordinary sentence being swallowed in a
    document whose headings are not set any larger than its body.
    """
    text = following.text.strip()
    if not text or not text[:1].islower():
        return False
    if abs(following.size - heading.size) > 0.1 or following.bold != heading.bold:
        return False
    return is_heading(following, body)


def page_heading(page: PageText, body: float) -> str | None:
    """The first heading on a page, if it opens with one.

    Only the top few lines are considered: a heading further down belongs to a
    section that starts mid-page, and chunks break on page boundaries.
    """
    for index, line in enumerate(page.lines[:4]):
        if not is_heading(line, body):
            continue
        parts = [line.text.strip()]
        for following in page.lines[index + 1:index + MAX_HEADING_LINES]:
            if not continues_heading(line, following, body):
                break
            parts.append(following.text.strip())
        return " ".join(parts)
    return None


def _chunk_from(pages: list[PageText], ordinal: int, title: str | None) -> Chunk:
    text = extract.normalize_whitespace("\n\n".join(page.text for page in pages))
    return Chunk(
        ordinal=ordinal,
        page_start=pages[0].number,
        page_end=pages[-1].number,
        text=text,
        title=title,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def chunk_pages(
    pages: list[PageText],
    min_words: int = 1500,
    max_words: int = 2500,
) -> list[Chunk]:
    """Groups pages into chunks, breaking on headings once the minimum is met.

    The size window is a preference, not a rule: a heading is only allowed to
    break a chunk once it holds `min_words`, and a chunk is force-broken at
    `max_words` whether a heading turned up or not.
    """
    body = body_size(pages)
    chunks: list[Chunk] = []
    current: list[PageText] = []
    current_words = 0
    current_title: str | None = None
    last_heading: str | None = None

    for page in pages:
        heading = page_heading(page, body)

        # A heading opening this page can close the previous chunk -- but only if
        # that chunk is already worth generating from.
        if heading and current and current_words >= min_words:
            chunks.append(_chunk_from(current, len(chunks), current_title))
            current, current_words, current_title = [], 0, None

        if heading:
            last_heading = heading
        if not current:
            # A chunk is titled by the heading it opens under, or failing that by
            # the last heading seen -- a section running across three pages keeps
            # its name on all three.
            current_title = heading or last_heading

        current.append(page)
        current_words += page.word_count

        if current_words >= max_words:
            chunks.append(_chunk_from(current, len(chunks), current_title))
            current, current_words, current_title = [], 0, None

    if current:
        chunks.append(_chunk_from(current, len(chunks), current_title))

    return [chunk for chunk in chunks if chunk.text.strip()]
