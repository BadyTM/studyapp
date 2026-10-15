"""Pulls text out of a PDF, one page at a time.

Two things come back per page: the plain text that ends up in a chunk, and a list
of typographic lines that the chunker reads to find chapter headings. Word
coordinates are deliberately not carried along -- they are needed once, when the
highlight rectangles are computed, and holding them for 200 pages is pointless.
"""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf


@dataclass
class Line:
    """One typographic line, with what the heading heuristic needs to judge it."""

    text: str
    size: float          # largest font size on the line, in points
    bold: bool
    page: int            # 0-based, matching PyMuPDF and QtPdf


@dataclass
class PageText:
    number: int          # 0-based
    text: str
    lines: list[Line] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_whitespace(text: str) -> str:
    """Tidies spacing without touching a single word.

    Words have to survive verbatim: the quote validation and the PDF highlighting
    both match against this text, so a "helpful" fix here breaks both.
    """
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _lines_from_dict(page_dict: dict, page_number: int) -> list[Line]:
    lines = []
    for block in page_dict.get("blocks", []):
        if block.get("type") != 0:          # 0 = text, 1 = image
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(span.get("text", "") for span in spans).strip()
            if not text:
                continue
            size = max((span.get("size", 0.0) for span in spans), default=0.0)
            # Bit 4 of the span flags is the bold bit in PyMuPDF.
            bold = any(span.get("flags", 0) & 16 for span in spans)
            lines.append(Line(text=text, size=round(size, 1), bold=bold, page=page_number))
    return lines


def extract_pages(path: str | Path) -> list[PageText]:
    """Reads every page of the PDF into text plus typographic lines."""
    pages = []
    with pymupdf.open(path) as document:
        for number, page in enumerate(document):
            text = normalize_whitespace(page.get_text("text"))
            lines = _lines_from_dict(page.get_text("dict"), number)
            pages.append(PageText(number=number, text=text, lines=lines))
    return pages


def page_count(path: str | Path) -> int:
    with pymupdf.open(path) as document:
        return document.page_count


def has_text_layer(pages: list[PageText], threshold: int = 20) -> bool:
    """Whether the PDF carries real text or is a stack of scanned images.

    Worth asking before anything else: a scan without a text layer needs OCR
    first, and every later phase -- quotes, page numbers, highlights -- rests on
    text that would not be there.
    """
    return any(page.word_count >= threshold for page in pages)
