"""Validation, against saved replies from the model.

The fixtures in `fixtures/replies/` are shaped like real answers, including the
three ways a reply goes wrong in practice: the quote comes back translated, the
quote comes back paraphrased, and the correct answer no longer names any of the
options. Each of those has to be caught here, because nothing downstream looks
at the question again.
"""

import json
import random
from pathlib import Path

import pytest

from studyapp.generate import validate
from studyapp.generate.validate import Rejection

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def chunk_text() -> str:
    return (FIXTURES / "chunk_transakce.txt").read_text(encoding="utf-8")


def reply(name: str) -> list[dict]:
    payload = json.loads((FIXTURES / "replies" / f"{name}.json").read_text(encoding="utf-8"))
    return payload["questions"]


def only_rejection(result: validate.Result) -> Rejection:
    assert len(result.rejected) == 1, result.reason_counts()
    return result.rejected[0].reason


class TestQuoteLocation:
    def test_exact_quote(self, chunk_text):
        quote = ("Trvalost znamená, že po úspěšném potvrzení transakce zůstávají "
                 "její změny v databázi zachovány")
        found = validate.locate_quote(quote, chunk_text)
        assert found is not None
        assert found.score == 1.0
        assert chunk_text[found.start:found.end].startswith("Trvalost znamená")

    def test_quote_spanning_a_line_break(self, chunk_text):
        # In the file this sentence is wrapped; the model sends it on one line.
        quote = ("Trvalost znamená, že po úspěšném potvrzení transakce zůstávají její "
                 "změny v databázi zachovány i v případě následného výpadku systému.")
        found = validate.locate_quote(quote, chunk_text)
        assert found is not None and found.score == 1.0

    def test_tidied_punctuation_still_matches(self, chunk_text):
        # A comma dropped and the spacing changed -- the plan's "model corrected
        # the quote" case, which fuzzy matching is there to absorb.
        quote = ("Atomicita znamená že dílčí operace transakce nelze pozorovat "
                 "samostatně. Buď se provedou všechny nebo žádná z nich.")
        found = validate.locate_quote(quote, chunk_text)
        assert found is not None
        assert 0.9 < found.score < 1.0

    def test_paraphrase_is_not_found(self, chunk_text):
        quote = ("Izolace je vlastnost, která zajišťuje, že paralelně probíhající "
                 "transakce se navzájem nijak neruší a výsledek je stejný.")
        assert validate.locate_quote(quote, chunk_text) is None

    def test_translation_is_not_found(self, chunk_text):
        quote = ("Durability means that after a transaction has been successfully "
                 "committed, its changes remain in the database.")
        assert validate.locate_quote(quote, chunk_text) is None

    def test_stored_quote_is_the_chunks_own_wording(self, chunk_text):
        # The model lower-cased the opening and lost a comma; what gets stored is
        # the source's version, so the PDF search has something to find.
        quote = ("trvalost znamená že po úspěšném potvrzení transakce zůstávají "
                 "její změny v databázi zachovány")
        found = validate.locate_quote(quote, chunk_text)
        assert found is not None
        assert found.text.startswith("Trvalost znamená, že")
        assert found.text in chunk_text

    def test_too_short_to_locate_honestly(self, chunk_text):
        assert validate.locate_quote("Atomicita", chunk_text) is None

    def test_empty_chunk(self):
        assert validate.locate_quote("cokoliv dlouhého co má přes třicet znaků", "") is None


class TestNormalisationMap:
    def test_offsets_point_back_into_the_original(self):
        original = "prvni\n\n  druhy   treti"
        collapsed, origin = validate.normalize_with_map(original)
        assert collapsed == "prvni druhy treti"
        assert original[origin[collapsed.index("druhy")]] == "d"
        assert original[origin[collapsed.index("treti")]] == "t"

    def test_map_and_text_stay_the_same_length(self):
        collapsed, origin = validate.normalize_with_map("  a \n b  \t c  ")
        assert len(collapsed) == len(origin)


class TestMultipleChoice:
    def test_correct_becomes_an_index(self, chunk_text):
        result = validate.validate_questions(reply("good"), chunk_text, shuffle=False)
        first = result.accepted[0]
        assert first["options"][first["correct_index"]] == "Trvalost"

    def test_rephrased_correct_answer_is_rejected(self, chunk_text):
        # The plan's reason for storing an index: string comparison would have
        # marked this right answer wrong every time it was asked.
        result = validate.validate_questions(reply("correct_mismatch"), chunk_text)
        assert only_rejection(result) is Rejection.CORRECT_NOT_IN_OPTIONS

    def test_three_options_is_rejected(self, chunk_text):
        raw = reply("good")[0]
        raw["options"] = raw["options"][:3]
        outcome = validate.validate_question(raw, chunk_text)
        assert outcome.reason is Rejection.WRONG_OPTION_COUNT

    def test_duplicate_options_are_rejected(self, chunk_text):
        result = validate.validate_questions(reply("mixed"), chunk_text)
        assert Rejection.DUPLICATE_OPTIONS.value in result.reason_counts()

    def test_shuffle_moves_the_answer_and_keeps_it_correct(self, chunk_text):
        # The model favours a position; a reader learns it without meaning to.
        raw = reply("good")[0]
        positions = set()
        for seed in range(20):
            row = validate.validate_question(
                dict(raw), chunk_text, shuffle=True, rng=random.Random(seed))
            assert row["options"][row["correct_index"]] == "Trvalost"
            positions.add(row["correct_index"])
        assert len(positions) > 1

    def test_shuffle_keeps_every_option(self, chunk_text):
        raw = reply("good")[0]
        row = validate.validate_question(dict(raw), chunk_text,
                                         rng=random.Random(1))
        assert sorted(row["options"]) == sorted(raw["options"])


class TestOpenQuestions:
    def test_answer_is_stored_as_text(self, chunk_text):
        result = validate.validate_questions(reply("good"), chunk_text)
        openq = [row for row in result.accepted if row["type"] == "open"][0]
        assert openq["correct_text"].startswith("Záznamy transakčního žurnálu")
        assert openq["options"] is None
        assert openq["correct_index"] is None


class TestWholeReplies:
    def test_a_clean_reply_passes_entirely(self, chunk_text):
        result = validate.validate_questions(reply("good"), chunk_text)
        assert result.rejected == []
        assert len(result.accepted) == 3
        assert result.pass_rate == 1.0

    def test_a_translated_quote_fails_the_whole_chunk(self, chunk_text):
        # Distinctive on purpose: a chunk that loses every question this way means
        # the prompt is not separating the two language rules clearly enough.
        result = validate.validate_questions(reply("translated_quote"), chunk_text)
        assert result.accepted == []
        assert only_rejection(result) is Rejection.QUOTE_NOT_FOUND

    def test_a_paraphrased_quote_is_rejected(self, chunk_text):
        result = validate.validate_questions(reply("paraphrased"), chunk_text)
        assert only_rejection(result) is Rejection.QUOTE_NOT_FOUND

    def test_a_realistic_mixed_reply(self, chunk_text):
        result = validate.validate_questions(reply("mixed"), chunk_text)
        assert len(result.accepted) == 2
        assert result.reason_counts() == {
            Rejection.CORRECT_NOT_IN_OPTIONS.value: 1,
            Rejection.QUOTE_NOT_FOUND.value: 1,
            Rejection.DUPLICATE_OPTIONS.value: 1,
            Rejection.EMPTY_QUESTION.value: 1,
        }

    def test_the_same_question_twice_is_kept_once(self, chunk_text):
        questions = reply("good")
        result = validate.validate_questions(questions + [dict(questions[0])], chunk_text)
        assert len(result.accepted) == 3
        assert only_rejection(result) is Rejection.DUPLICATE_QUESTION

    def test_an_empty_reply_is_not_an_error(self, chunk_text):
        # A contents page genuinely has nothing to ask about.
        result = validate.validate_questions([], chunk_text)
        assert result.accepted == [] and result.rejected == []

    def test_none_is_tolerated(self, chunk_text):
        assert validate.validate_questions(None, chunk_text).accepted == []


class TestMalformedInput:
    @pytest.mark.parametrize("raw,expected", [
        ({"type": "essay", "question": "q", "correct": "a", "source_quote": "x" * 40},
         Rejection.UNKNOWN_TYPE),
        ({"type": "open", "question": "", "correct": "a", "source_quote": "x" * 40},
         Rejection.EMPTY_QUESTION),
        ({"type": "open", "question": "q", "correct": "", "source_quote": "x" * 40},
         Rejection.MISSING_FIELD),
        ({"type": "open", "question": "q", "correct": "a", "source_quote": ""},
         Rejection.MISSING_FIELD),
        ({"type": "open", "question": "q", "correct": "a", "source_quote": "krátká"},
         Rejection.QUOTE_TOO_SHORT),
    ])
    def test_bad_shapes_are_named_not_crashed(self, raw, expected, chunk_text):
        assert validate.validate_question(raw, chunk_text).reason is expected

    def test_missing_keys_entirely(self, chunk_text):
        assert isinstance(validate.validate_question({}, chunk_text), validate.Rejected)


class TestCaseFoldingDoesNotShiftTheQuote:
    """`str.casefold()` is not length-preserving.

    `ﬁ` folds to two characters, `ß` to `ss`, `İ` to two. A position found in
    the folded string does not index the unfolded one, so every such character
    before a match slid the stored quote along by one: "Atomicita ..." came out
    as "tomicita ...", with a score of 1.0 and nothing to say it was wrong.

    Ligatures are ordinary in typeset PDFs and `ß` in German, so this is not an
    exotic input -- it is most of a German textbook.
    """

    def quote_from(self, prefix):
        quote = "Atomicita znamena, ze dilci operace transakce nelze pozorovat."
        chunk = f"Strana {prefix} uvodni poznamka. {quote}"
        return quote, validate.locate_quote(quote, chunk)

    @pytest.mark.parametrize("character", ["\ufb01", "\ufb02", "ß", "İ", "ﬄ"])
    def test_a_character_that_folds_longer_does_not_move_the_quote(self, character):
        quote, located = self.quote_from(character)
        assert located is not None
        assert located.text == quote, f"{character!r} shifted the stored quote"

    def test_many_of_them_do_not_stack_up(self, character="ß"):
        quote, located = self.quote_from(character * 8)
        assert located.text == quote

    def test_the_offsets_point_at_the_quote_in_the_original(self):
        quote, located = self.quote_from("\ufb01")
        chunk = f"Strana \ufb01 uvodni poznamka. {quote}"
        assert chunk[located.start:located.end] == quote

    def test_ordinary_text_is_unaffected(self):
        quote, located = self.quote_from("x")
        assert located.text == quote and located.score == 1.0

    def test_folding_still_matches_case_insensitively(self):
        chunk = "ATOMICITA ZNAMENA, ZE DILCI OPERACE TRANSAKCE NELZE POZOROVAT."
        located = validate.locate_quote(
            "atomicita znamena, ze dilci operace transakce nelze pozorovat.", chunk)
        assert located is not None and located.text == chunk


class TestAnApproximateMatchStaysInsideItsSentence:
    """`_best_window` reports where to look; it does not say how much to keep.

    The window is as long as the quote the model sent, which is not the same as
    the run of source it matched. A quote with a word added slid the window
    backwards to make up the length, and the stored text began in the paragraph
    before -- the tail of the previous sentence, then the quote, at 0.92.
    """

    CHUNK = ("Prvni odstavec konci tady slovem dokumentu.\n\n"
             "Trvalost znamena, ze zmeny zustavaji zachovany i po vypadku systemu.")
    SENTENCE = "Trvalost znamena, ze zmeny zustavaji zachovany i po vypadku systemu."

    def test_a_quote_with_a_word_added_does_not_reach_back(self):
        located = validate.locate_quote(
            "Trvalost totiz znamena, ze zmeny zustavaji zachovany i po vypadku systemu.",
            self.CHUNK)
        assert located is not None
        assert located.text.startswith("Trvalost")
        assert "dokumentu" not in located.text

    def test_a_typo_is_still_matched(self):
        located = validate.locate_quote(
            "Trvalost znamena, ze zmeny zustavajii zachovany i po vypadku systemu.",
            self.CHUNK)
        assert located is not None and located.text.startswith("Trvalost")

    def test_an_exact_quote_is_unaffected(self):
        located = validate.locate_quote(self.SENTENCE, self.CHUNK)
        assert located.text == self.SENTENCE and located.score == 1.0

    def test_a_paraphrase_is_still_refused(self):
        # The trimming must not turn a rejection into a partial acceptance.
        assert validate.locate_quote(
            "Zmeny po potvrzeni transakce uz nikdy nezmizi ani pri havarii.",
            self.CHUNK) is None

    def test_the_stored_text_is_a_real_slice_of_the_chunk(self):
        located = validate.locate_quote(
            "Trvalost totiz znamena, ze zmeny zustavaji zachovany i po vypadku systemu.",
            self.CHUNK)
        assert located.text in self.CHUNK
        assert self.CHUNK[located.start:located.end].strip() == located.text


class TestAStoredQuoteBeginsAndEndsOnWholeWords:
    """An approximate match lines up on characters, not on words.

    So a span could start after the first letters of a word -- `žurnál` stored
    as `rnál`. Still a literal run of the source, which is the rule that counts,
    and still damage: it reads as a typo in the question and the marker drawn
    over the page starts mid-word.

    Found by fuzzing rather than by reading: 23 cases in 4000 random chunks.
    """

    CHUNK = ("První odstavec zmiňuje žurnál a jeho záznamy. "
             "Trvalost znamená, že změny zůstávají zachovány i po výpadku.")

    def located(self, quote):
        return validate.locate_quote(quote, self.CHUNK)

    def test_a_match_starting_mid_word_is_widened(self):
        # The quote the model sent begins inside "žurnál".
        located = self.located("urnál a jeho záznamy. Trvalost znamená, že změny")
        if located is None:
            pytest.skip("this quote does not match at all")
        assert located.text.startswith("žurnál"), located.text[:30]

    def test_a_match_ending_mid_word_is_widened(self):
        located = self.located("Trvalost znamená, že změny zůstávají zachová")
        if located is None:
            pytest.skip("this quote does not match at all")
        assert located.text.endswith("zachovány"), located.text[-30:]

    @pytest.mark.parametrize("quote", [
        "Trvalost znamená, že změny zůstávají zachovány i po výpadku.",
        "zmiňuje žurnál a jeho záznamy. Trvalost znamená",
        "První odstavec zmiňuje žurnál a jeho záznamy.",
    ])
    def test_an_exact_quote_is_not_widened(self, quote):
        located = self.located(quote)
        assert located is not None
        assert located.text == quote, "a whole-word quote was extended"

    def test_the_widened_quote_is_still_a_literal_slice(self):
        located = self.located("urnál a jeho záznamy. Trvalost znamená, že změny")
        if located is None:
            pytest.skip("this quote does not match at all")
        assert located.text in self.CHUNK
        assert self.CHUNK[located.start:located.end].strip() == located.text

    def test_punctuation_is_not_swallowed(self):
        # Widening crosses letters, not commas: it must not eat its way into
        # the sentence before.
        located = self.located("Trvalost znamená, že změny zůstávají zachovány")
        assert located is not None
        assert not located.text.startswith("záznamy")
