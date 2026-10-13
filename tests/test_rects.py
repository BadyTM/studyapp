"""Locating a quote in the PDF -- the pass that yields both page and highlight.

Built against a real PDF rather than a stub, because the thing being tested is
exactly what a PDF does to text: wrap it mid-sentence, break a word across a
line, and justify the spacing. A mock would agree with the code and prove nothing.
"""

import pymupdf
import pytest

from studyapp.generate import rects

PAGE_ONE = (
    "6.2.3 Izolace\n\n"
    "Izolace řeší vzájemné ovlivňování souběžně běžících transakcí. V ideálním "
    "případě se každá transakce chová tak, jako by běžela v systému sama.\n\n"
    "Míru izolace lze nastavovat pomocí izolačních úrovní, přičemž nižší úroveň "
    "znamená vyšší propustnost za cenu rizika nekonzistentního čtení."
)

PAGE_TWO = (
    "6.2.4 Trvalost\n\n"
    "Poslední z vlastností ACID se týká chování systému po potvrzení transakce. "
    "Zatímco izolace se zabývá souběžným během, trvalost cílí na odolnost vůči "
    "výpadku.\n\n"
    "Trvalost znamená, že po úspěšném potvrzení transakce zůstávají její změny "
    "v databázi zachovány i v případě následného výpadku systému. Prakticky se "
    "zajišťuje zápisem záznamů transakčního žurnálu na trvalé médium dříve, než "
    "je klientovi potvrzeno provedení operace."
)


@pytest.fixture(scope="module")
def pdf(tmp_path_factory) -> str:
    """A three-page PDF: a title page, then the two pages of text above.

    The narrow text box is deliberate -- it forces the wrapping that makes
    `search_for` fail and the word-level fallback earn its place.
    """
    path = tmp_path_factory.mktemp("pdf") / "transakce.pdf"
    document = pymupdf.open()

    first = document.new_page()
    first.insert_text((72, 300), "Databázové systémy", fontsize=24, fontname="hebo")

    for content in (PAGE_ONE, PAGE_TWO):
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(72, 72, 380, 700), content,
                            fontsize=11, fontname="helv")

    document.save(path)
    document.close()
    return str(path)


@pytest.fixture(scope="module")
def document(pdf):
    with pymupdf.open(pdf) as opened:
        yield opened


class TestMergingRects:
    def test_words_on_one_line_become_one_rectangle(self):
        boxes = [(72.0, 100.0, 110.0, 112.0), (114.0, 100.5, 160.0, 112.0)]
        assert rects.merge_line_rects(boxes) == [[72.0, 100.0, 160.0, 112.0]]

    def test_two_lines_stay_two_rectangles(self):
        boxes = [(72.0, 100.0, 160.0, 112.0), (72.0, 118.0, 140.0, 130.0)]
        assert len(rects.merge_line_rects(boxes)) == 2

    def test_nothing_in_nothing_out(self):
        assert rects.merge_line_rects([]) == []


class TestLocateOnPage:
    def test_short_quote_on_one_line(self, document):
        score, found = rects.locate_on_page(
            document[1], "Izolace řeší vzájemné ovlivňování")
        assert found and score > 0

    def test_quote_that_wraps_across_lines(self, document):
        # This is the case `search_for` alone cannot do, and the reason the word
        # fallback exists: the sentence is broken by the text box, not the source.
        quote = ("Trvalost znamená, že po úspěšném potvrzení transakce zůstávají její "
                 "změny v databázi zachovány i v případě následného výpadku systému.")
        score, found = rects.locate_on_page(document[2], quote)
        assert len(found) >= 2, "a wrapped quote should give one rect per line"
        assert score >= rects.WORD_MATCH_THRESHOLD

    def test_absent_quote_gives_nothing(self, document):
        score, found = rects.locate_on_page(
            document[1], "Toto v dokumentu vůbec není napsáno.")
        assert found == [] and score == 0.0

    def test_empty_quote(self, document):
        assert rects.locate_on_page(document[1], "   ") == (0.0, [])

    def test_rectangles_lie_inside_the_page(self, document):
        _, found = rects.locate_on_page(document[2], "Prakticky se zajišťuje zápisem")
        box = document[2].rect
        for x0, y0, x1, y1 in found:
            assert 0 <= x0 < x1 <= box.width
            assert 0 <= y0 < y1 <= box.height


class TestLocate:
    def test_finds_the_page_the_quote_is_on(self, document):
        page, found = rects.locate(document, 0, 2,
                                   "trvalost cílí na odolnost vůči výpadku")
        assert page == 2 and found

    def test_the_page_comes_from_the_search_not_from_a_guess(self, document):
        page, _ = rects.locate(document, 0, 2, "Míru izolace lze nastavovat pomocí")
        assert page == 1

    def test_a_quote_outside_the_chunks_pages_is_not_found(self, document):
        # The search is confined to the chunk's own pages on purpose: widening it
        # trades a missing highlight for a confidently wrong one.
        page, found = rects.locate(document, 0, 0, "trvalost cílí na odolnost")
        assert page is None and found == []

    def test_a_missing_quote_returns_none_rather_than_a_page(self, document):
        page, found = rects.locate(document, 0, 2, "Zcela vymyšlená věta o něčem jiném.")
        assert page is None and found == []

    def test_page_range_past_the_end_does_not_crash(self, document):
        page, _ = rects.locate(document, 0, 99, "Míru izolace lze nastavovat")
        assert page == 1


class TestWordFallback:
    def test_matches_over_the_word_list(self, document):
        words = document[2].get_text("words")
        score, found = rects.match_over_words(
            words, "zajišťuje zápisem záznamů transakčního žurnálu")
        assert found and score >= rects.WORD_MATCH_THRESHOLD

    def test_no_words_no_rects(self):
        assert rects.match_over_words([], "cokoliv") == (0.0, [])

    def test_empty_quote_gives_nothing(self, document):
        assert rects.match_over_words(document[2].get_text("words"), "") == (0.0, [])


def page_with(tmp_path, name, *texts, width=540):
    """A PDF with one page per text, each in a box of the given width."""
    path = tmp_path / name
    document = pymupdf.open()
    for text in texts:
        document.new_page().insert_textbox(
            pymupdf.Rect(60, 72, 60 + width, 740), text,
            fontsize=11, fontname="helv")
    document.save(path)
    document.close()
    return path


LONG = ("Trvalost znamena, ze po uspesnem potvrzeni transakce zustavaji jeji "
        "zmeny v databazi zachovany i v pripade nasledneho vypadku systemu "
        "nebo vypadku napajeni.")


class TestTheMarkerCoversTheWholeQuote:
    """`search_for(quote[:80])` was the quickest way to find the page and also
    what painted the marker, so a 200-character quote was highlighted for its
    first eighty characters and stopped, mid-word. Measured over a real bank,
    176 of 180 questions were marked that way.
    """

    def covered(self, boxes):
        return sum(box[2] - box[0] for box in boxes) / 5.1   # ~chars at 11pt

    def test_a_long_quote_on_one_line_is_covered_end_to_end(self, tmp_path):
        path = page_with(tmp_path, "one.pdf", LONG, width=480)
        with pymupdf.open(path) as document:
            page, boxes = rects.locate(document, 0, 0, LONG)
        assert self.covered(boxes) > len(LONG) * 0.75

    def test_a_quote_wrapped_over_lines_gets_a_rect_per_line(self, tmp_path):
        path = page_with(tmp_path, "narrow.pdf", LONG, width=140)
        with pymupdf.open(path) as document:
            page, boxes = rects.locate(document, 0, 0, LONG)
        assert len(boxes) >= 3
        assert self.covered(boxes) > len(LONG) * 0.75

    def test_a_short_quote_is_not_over_covered(self, tmp_path):
        short = "Trvalost znamena, ze zmeny zustavaji zachovany."
        path = page_with(tmp_path, "short.pdf", f"Uvod. {short} Zaver.")
        with pymupdf.open(path) as document:
            page, boxes = rects.locate(document, 0, 0, short)
        assert self.covered(boxes) < len(short) * 1.6


class TestTheBestPageWinsNotTheFirst:
    """Taking the first page that answered at all let a six-word opening on an
    earlier page steal the page number from the page that held the quote -- and
    the six-word highlight came with it.
    """

    def test_an_earlier_echo_does_not_steal_the_page(self, tmp_path):
        echo = "Trvalost znamena, ze po uspesnem potvrzeni nelze data ztratit."
        path = page_with(tmp_path, "echo.pdf", echo, LONG, width=200)
        with pymupdf.open(path) as document:
            page, boxes = rects.locate(document, 0, 1, LONG)
        assert page == 1

    def test_a_negated_near_duplicate_does_not_win(self, tmp_path):
        # The worst of them: the decoy says the opposite of the answer, and
        # scores 0.94 against the quote on text alone.
        decoy = ("Trvanlivost znamena, ze po neuspesnem potvrzeni transakce "
                 "nezustavaji jeji zmeny v databazi zachovany i v pripade "
                 "nasledneho vypadku serveru.")
        path = page_with(tmp_path, "decoy.pdf", decoy, LONG, width=200)
        with pymupdf.open(path) as document:
            page, boxes = rects.locate(document, 0, 1, LONG)
            under = " ".join(
                word[4] for word in document[page].get_text("words")
                if any(box[0] - 1 <= word[0] and word[2] <= box[2] + 1
                       and box[1] - 1 <= word[1] and word[3] <= box[3] + 1
                       for box in boxes))
        assert page == 1
        assert "neuspesnem" not in under, "the marker is on the negated sentence"

    def test_the_real_page_still_wins_when_it_comes_first(self, tmp_path):
        echo = "Trvalost znamena, ze po uspesnem potvrzeni nelze data ztratit."
        path = page_with(tmp_path, "first.pdf", LONG, echo, width=200)
        with pymupdf.open(path) as document:
            page, _ = rects.locate(document, 0, 1, LONG)
        assert page == 0

    def test_a_quote_on_no_page_is_still_reported_as_missing(self, tmp_path):
        path = page_with(tmp_path, "none.pdf", "Uplne jiny text bez souvislosti.")
        with pymupdf.open(path) as document:
            page, boxes = rects.locate(document, 0, 0, LONG)
        assert page is None and boxes == []


class TestColumnsAreNotMergedIntoOneBand:
    """Two columns share every baseline on the page.

    Merging on the baseline alone put a word from each into one rectangle
    straight through the gutter -- a highlight over text that was never quoted.
    """

    def test_a_gutter_is_not_a_space(self):
        merged = rects.merge_line_rects([(60, 200, 140, 212), (310, 200.2, 400, 212)])
        assert len(merged) == 2, "the two columns were joined into one band"

    def test_words_on_one_line_still_merge(self):
        merged = rects.merge_line_rects([(60, 200, 140, 212), (144, 200.1, 210, 212)])
        assert len(merged) == 1
        assert merged[0][0] == 60 and merged[0][2] == 210

    def test_different_lines_stay_apart(self):
        merged = rects.merge_line_rects([(60, 200, 140, 212), (60, 230, 140, 242)])
        assert len(merged) == 2

    def test_a_single_box_is_returned_as_is(self):
        assert rects.merge_line_rects([(60, 200, 140, 212)]) == [[60, 200, 140, 212]]
