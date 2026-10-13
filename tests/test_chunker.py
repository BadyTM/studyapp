"""Chunking is where a bad decision stays invisible longest.

A chunk that straddles two chapters still generates plausible questions; it just
quietly makes the page-range search wider than it should be and the chapter title
wrong. So the tests check the boundaries, not the output.
"""

import pytest

from studyapp.ingest import chunker
from studyapp.ingest.chunker import (
    body_size,
    chunk_pages,
    is_heading,
    page_heading,
)
from studyapp.ingest.extract import Line, PageText, normalize_whitespace


def line(text: str, size: float = 11.0, bold: bool = False, page: int = 0) -> Line:
    return Line(text=text, size=size, bold=bold, page=page)


def page(number: int, words: int, heading: str | None = None) -> PageText:
    body = " ".join(["slovo"] * words)
    lines = [line(heading, size=18.0, page=number)] if heading else []
    lines.append(line(body, size=11.0, page=number))
    text = f"{heading}\n{body}" if heading else body
    return PageText(number=number, text=text, lines=lines)


class TestHeadingDetection:
    def test_multilevel_numbering_stands_on_its_own(self):
        # "8.3.1" is distinctive enough that no font evidence is needed.
        assert is_heading(line("8.3.1 Clusterovany index"), body=11.0)

    def test_a_bare_number_at_body_size_is_not_a_heading(self):
        # From a real document: "5 lumbar, 5 sacral and 1 coccygeal..." is an
        # ordinary sentence that happened to wrap onto its own line, and a
        # numbered list item reads the same as a numbered heading. The font
        # decides; text alone does not.
        assert not is_heading(line("6 Transakce"), body=11.0)

    def test_the_same_line_in_a_heading_font_is_one(self):
        assert is_heading(line("6 Transakce", size=16.0), body=11.0)

    def test_bold_at_body_size_is_enough(self):
        assert is_heading(line("6 Transakce", size=11.0, bold=True), body=11.0)

    def test_a_wrapped_sentence_beginning_with_a_number(self):
        # The exact false positive that motivated the rule: it titled a chunk
        # after a sentence fragment and broke the chunk in the wrong place.
        text = "5 lumbar, 5 sacral and 1 coccygeal. They are formed by a"
        assert not is_heading(line(text), body=10.0)

    def test_a_numbered_list_item(self):
        assert not is_heading(line("1. Mechanotransduction"), body=10.0)

    def test_two_sentences_on_one_line_are_never_a_heading(self):
        # Even set large: a heading does not contain a full stop mid-line.
        assert not is_heading(line("Krátká věta. A další.", size=18.0), body=11.0)

    def test_larger_font_alone_is_enough(self):
        assert is_heading(line("Transakce", size=18.0), body=11.0)

    def test_all_caps(self):
        assert is_heading(line("VLASTNOSTI TRANSAKCI"), body=11.0)

    def test_running_text_is_not_a_heading(self):
        sentence = ("Transakce je posloupnost operaci nad databazi, ktera se chova "
                    "jako jedina nedelitelna jednotka prace.")
        assert not is_heading(line(sentence), body=11.0)

    def test_long_line_in_a_big_font_is_not_a_heading(self):
        # A pull quote or an opening paragraph set large: big, but far too long.
        text = " ".join(["slovo"] * 20)
        assert not is_heading(line(text, size=18.0), body=11.0)

    def test_sentence_ending_in_a_full_stop_is_not_a_heading(self):
        assert not is_heading(line("Toto je kratka veta.", size=18.0), body=11.0)

    def test_numbered_heading_may_end_in_a_full_stop(self):
        assert is_heading(line("4.4.", size=11.0), body=11.0)

    def test_heading_is_only_looked_for_at_the_top_of_a_page(self):
        # A heading further down starts a section mid-page, and chunks break on
        # page boundaries -- treating it as one would mistitle the chunk.
        body_lines = [line("bezny text v odstavci ktery pokracuje dal a dal") for _ in range(6)]
        deep = PageText(number=3, text="x", lines=body_lines + [line("9 Zaver", size=18.0)])
        assert page_heading(deep, body=11.0) is None


class TestBodySize:
    def test_median_ignores_the_occasional_heading(self):
        pages = [PageText(0, "x", [line("t", 11.0)] * 40 + [line("H", 18.0)] * 2)]
        assert body_size(pages) == 11.0

    def test_no_lines_at_all(self):
        assert body_size([PageText(0, "", [])]) == 0.0


class TestAHeadingSetAcrossTwoLines:
    """Taken from the book this was built for, where two chapters of 23 were cut.

    A heading that wraps arrives as two lines and nothing marks the second as a
    continuation. Joining every heading that follows another would be worse than
    the cut, though: page 1 of that same book carries its title and its first
    chapter heading in the same 22pt bold, one under the other.
    """

    def _page(self, *lines):
        return PageText(number=0, text="x", lines=list(lines) + [line("body " * 20)])

    def test_the_rest_of_the_heading_is_kept(self):
        page_text = self._page(
            line("Recording of neuronal and glial activity - extracellular and",
                 size=16.0, bold=True),
            line("intracellular recordings", size=16.0, bold=True))

        assert page_heading(page_text, body=10.0) == (
            "Recording of neuronal and glial activity - extracellular and "
            "intracellular recordings")

    def test_a_second_heading_is_left_alone(self):
        # The document title, then the first chapter. Same size, same weight,
        # one under the other -- and two different things.
        page_text = self._page(
            line("State exam - notes", size=22.0, bold=True),
            line("Structure and function of the cell membrane", size=22.0,
                 bold=True))

        assert page_heading(page_text, body=10.0) == "State exam - notes"

    def test_a_smaller_line_underneath_is_a_different_heading(self):
        page_text = self._page(
            line("The role of the thalamus", size=16.0, bold=True),
            line("and its nuclei", size=12.0, bold=True))

        assert page_heading(page_text, body=10.0) == "The role of the thalamus"

    def test_body_text_is_not_swallowed(self):
        # The dangerous case: a document whose headings are not set any larger
        # than its body, where the line underneath is an ordinary sentence that
        # happens to start lowercase.
        page_text = self._page(
            line("8.3.1 Axonal transport", size=11.0),
            line("and this is where the body text begins, lowercase and long "
                 "enough to be nobody's heading", size=11.0))

        assert page_heading(page_text, body=11.0) == "8.3.1 Axonal transport"

    def test_three_lines_is_as_far_as_it_goes(self):
        page_text = self._page(
            line("The control of movement", size=16.0, bold=True),
            line("and the basal ganglia", size=16.0, bold=True),
            line("and the cerebellum", size=16.0, bold=True),
            line("and something else again", size=16.0, bold=True))

        assert page_heading(page_text, body=10.0) == (
            "The control of movement and the basal ganglia and the cerebellum")

    def test_the_chunk_carries_the_whole_title(self):
        """What the reader actually sees: the title on the card and in Progress."""
        wrapped = PageText(number=0, text="x", lines=[
            line("Recording of neuronal activity - extracellular and",
                 size=16.0, bold=True),
            line("intracellular recordings", size=16.0, bold=True),
            line("body " * 60)])
        chunks = chunk_pages([wrapped], min_words=10, max_words=400)

        assert chunks[0].title == ("Recording of neuronal activity - "
                                   "extracellular and intracellular recordings")


class TestChunking:
    def test_breaks_on_a_heading_once_the_minimum_is_met(self):
        pages = [
            page(0, 800, heading="1 Uvod"),
            page(1, 800),
            page(2, 800, heading="2 Relacni model"),   # 1600 words behind us
            page(3, 800),
        ]
        chunks = chunk_pages(pages, min_words=1500, max_words=2500)
        assert len(chunks) == 2
        assert (chunks[0].page_start, chunks[0].page_end) == (0, 1)
        assert (chunks[1].page_start, chunks[1].page_end) == (2, 3)

    def test_a_heading_below_the_minimum_does_not_break(self):
        pages = [
            page(0, 400, heading="1 Uvod"),
            page(1, 400, heading="2 Relacni model"),   # only 400 words so far
            page(2, 400),
        ]
        chunks = chunk_pages(pages, min_words=1500, max_words=2500)
        assert len(chunks) == 1
        assert chunks[0].page_end == 2

    def test_force_break_at_the_maximum_without_a_heading(self):
        pages = [page(number, 900) for number in range(6)]
        chunks = chunk_pages(pages, min_words=1500, max_words=2500)
        assert len(chunks) > 1
        assert all(chunk.word_count <= 2500 + 900 for chunk in chunks)

    def test_ordinals_are_contiguous_from_zero(self):
        pages = [page(number, 900) for number in range(9)]
        chunks = chunk_pages(pages, min_words=1500, max_words=2500)
        assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))

    def test_page_ranges_cover_every_page_exactly_once(self):
        pages = [page(number, 700, heading="Kapitola" if number % 4 == 0 else None)
                 for number in range(12)]
        chunks = chunk_pages(pages, min_words=1500, max_words=2500)
        covered = [number for chunk in chunks
                   for number in range(chunk.page_start, chunk.page_end + 1)]
        assert covered == list(range(12))

    def test_a_section_running_across_pages_keeps_its_title(self):
        pages = [
            page(0, 900, heading="6 Transakce"),
            page(1, 900),
            page(2, 900),      # forced break lands here, still inside chapter 6
            page(3, 900),
        ]
        chunks = chunk_pages(pages, min_words=1500, max_words=2500)
        assert len(chunks) >= 2
        assert all(chunk.title == "6 Transakce" for chunk in chunks)

    def test_pages_before_the_first_heading_have_no_title(self):
        pages = [page(0, 900), page(1, 900), page(2, 900)]
        chunks = chunk_pages(pages, min_words=1500, max_words=2500)
        assert chunks[0].title is None

    def test_empty_input(self):
        assert chunk_pages([], 1500, 2500) == []

    def test_identical_text_hashes_identically(self):
        # chunks.sha256 is what decides whether a chunk needs regenerating; if it
        # moved with anything but the text, every run would regenerate everything.
        first = chunk_pages([page(0, 900), page(1, 900)], 1500, 2500)
        second = chunk_pages([page(0, 900), page(1, 900)], 1500, 2500)
        assert first[0].sha256 == second[0].sha256

    def test_changed_text_changes_the_hash(self):
        first = chunk_pages([page(0, 900), page(1, 900)], 1500, 2500)
        second = chunk_pages([page(0, 900), page(1, 901)], 1500, 2500)
        assert first[0].sha256 != second[0].sha256


class TestWhitespaceNormalisation:
    def test_collapses_runs_of_spaces(self):
        assert normalize_whitespace("a    b") == "a b"

    def test_keeps_paragraph_breaks(self):
        assert normalize_whitespace("a\n\n\n\nb") == "a\n\nb"

    def test_does_not_alter_words(self):
        # The quote validation matches against this text character by character,
        # so anything that rewrites a word breaks it.
        source = "roz-\ndeleni slova a diakritika: příliš žluťoučký kůň"
        result = normalize_whitespace(source)
        assert "roz-" in result
        assert "příliš žluťoučký kůň" in result

    def test_non_breaking_space_becomes_a_normal_one(self):
        assert normalize_whitespace("a b") == "a b"


@pytest.mark.parametrize("text,expected", [
    ("cs", "Czech"),
    ("en", "English"),
    (None, "unknown"),
    ("xx", "unknown"),
])
def test_language_names(text, expected):
    from studyapp.ingest import language
    assert language.name(text) == expected


class TestTheProseBreakHeuristic:
    """Two bugs in one character class.

    `[A-ZÁ-Ž]` is U+0041-005A plus U+00C1-017D, and that second range is half
    lowercase -- `á`, `č`, `ř`, `ě` all live in it -- so "operaci. ale" read as a
    sentence break. And the dot in "1. Úvod" is not a full stop: the standard
    Czech heading form was being thrown out as prose, so such a heading never
    broke a chunk and never gave one its title.
    """

    @pytest.mark.parametrize("heading", [
        "1. Úvod do databázových systémů",
        "6.2. Vlastnosti transakcí",
        "8.3.1. Zotavení po havárii",
        "2 Relační model",
        "Název kapitoly bez čísla",
    ])
    def test_a_heading_is_not_prose(self, heading):
        assert not chunker.has_prose_break(heading)

    @pytest.mark.parametrize("prose", [
        "Konec věty. Další věta zde.",
        "Transakce proběhne. Potom se potvrdí.",
        "Je to tak! Opravdu ano.",
        "Platí to? Ano, platí.",
    ])
    def test_real_prose_still_reads_as_prose(self, prose):
        assert chunker.has_prose_break(prose)

    @pytest.mark.parametrize("lowercase", ["á", "č", "ř", "ě", "ž", "ý", "í"])
    def test_a_lowercase_accent_does_not_start_a_new_sentence(self, lowercase):
        # Every one of these sits inside the old A-Ž range.
        assert not chunker.has_prose_break(f"provedou operaci. {lowercase}le pak")

    def test_a_numbered_heading_with_a_sentence_after_it_is_still_prose(self):
        # Stripping the number must not make the rest of the line invisible.
        assert chunker.has_prose_break("1. Úvod. Tato kapitola popisuje vše.")


class TestLanguageMarkersActuallySeparate:
    """The marker sets listed every accented letter each language uses, so both
    matched any Czech or Slovak text and the 1.5x bonus went to both -- a
    tie-break that broke no tie.
    """

    def test_czech_and_slovak_markers_do_not_overlap(self):
        from studyapp.ingest import language

        assert not (language.MARKERS["cs"] & language.MARKERS["sk"])

    @pytest.mark.parametrize("text,expected", [
        ("Transakce je posloupnost operací nad databází, která se chová jako "
         "jediná nedělitelná jednotka práce. Atomicita znamená, že dílčí operace "
         "nelze pozorovat samostatně, a to ani při selhání systému. " * 12, "cs"),
        ("Transakcia je postupnosť operácií nad databázou, ktorá sa správa ako "
         "jediná nedeliteľná jednotka práce. Atomickosť znamená, že čiastkové "
         "operácie nemožno pozorovať samostatne pri zlyhaní systému. " * 12, "sk"),
    ])
    def test_the_right_language_still_wins(self, text, expected):
        from studyapp.ingest import language

        assert language.detect(text) == expected
