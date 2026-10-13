"""The prompts.

Tests on prompt text are worth having here because this project has already been
bitten twice by prompt content going wrong in ways nothing else could catch: the
shared prompt once told the CLI backend to call a tool that does not exist on
that path, and the output contract lost to two thousand words of passage until
it was repeated after the text rather than only before it.

These do not judge wording. They check that the instructions which cost real
generation runs to discover are still present, and still in the position that
made them hold.
"""

import pytest

from studyapp.generate import prompts


@pytest.fixture(scope="module")
def system() -> str:
    return prompts.system_prompt("English", "English", 2, 8, prompts.JSON_OUTPUT)


@pytest.fixture(scope="module")
def reminder() -> str:
    return prompts.json_reminder(2, 8)


class TestOutputContract:
    def test_the_api_path_is_told_about_the_tool(self):
        assert "save_questions" in prompts.system_prompt("English", "English")

    def test_the_cli_path_is_not(self):
        # It has no tool to call. Telling it to use one produced neither a tool
        # call nor JSON -- it wrote a markdown exam paper instead.
        cli = prompts.system_prompt("English", "English", 2, 8, prompts.JSON_OUTPUT)
        assert "save_questions" not in cli
        assert '"questions"' in cli

    def test_the_contract_is_restated_after_the_passage(self, reminder):
        # In the system prompt alone it lost to the passage, twice.
        assert '"type"' in reminder and '"source_quote"' in reminder
        assert "mcq" in reminder and "open" in reminder

    def test_the_reminder_forbids_the_vocabulary_it_reverted_to(self, reminder):
        # It answered with multiple_choice, marks, difficulty and options keyed
        # by letter until each was ruled out by name.
        for wrong in ('"multiple_choice"', '"marks"', '"difficulty"', '"section"'):
            assert wrong.strip('"') in reminder

    def test_no_stray_double_braces_reach_the_model(self, system, reminder):
        # JSON_OUTPUT is substituted into a format string as a value, and
        # str.format does not format its own substitutions -- doubling the
        # braces there would put literal {{ in front of the model.
        assert "{{" not in system and "{{" not in reminder


class TestQuestionsAreNotGameable:
    """The two tells measured on a real run of 33 multiple-choice questions.

    The correct option was the longest 58% of the time against 25% by chance, so
    picking the longest and reading nothing scored 58%. Absolutes appeared only
    in the wrong options in a third of them, giving a second free rule.
    """

    def test_the_options_are_told_to_match_in_length(self, system):
        assert "same length" in system
        assert "longest" in system

    def test_that_instruction_is_repeated_after_the_passage(self, reminder):
        # Where the format contract had to go before it would hold.
        assert "longest" in reminder

    def test_absolutes_may_not_mark_the_wrong_answers(self, system):
        for word in ("always", "never", "only", "cannot"):
            assert word in system

    def test_a_distractor_is_made_wrong_by_substitution(self, system):
        # The first attempt only said "even the absolutes out", which fought the
        # habit without replacing it: overstating a true claim is simply the
        # quickest way to write a false option. Naming the better technique --
        # swap a specific fact -- is what gives it somewhere else to go.
        assert "substituting a specific fact" in system
        assert "overstating" in system

    def test_that_technique_is_repeated_after_the_passage(self, reminder):
        assert "swapped" in reminder and "overstated" in reminder

    def test_the_correct_option_may_still_be_an_absolute_one(self, system):
        # Otherwise the rule inverts: never picking the absolute option becomes
        # the giveaway instead, and a passage that genuinely says "never" can no
        # longer be asked about.
        assert "fine for the correct option to be the absolute one" in system


class TestTheRulesThatAnchorEverything:
    def test_the_quote_must_be_copied_not_written(self, system):
        assert "character for character" in system

    def test_the_quote_stays_in_the_source_language(self, system):
        # Translating it breaks the validation and the highlight at once.
        assert "language of the source" in system

    def test_no_page_numbers_are_asked_of_the_model(self, system):
        # It cannot see them, so it would invent them.
        assert "not asked for any" in system

    def test_an_empty_answer_is_allowed(self, system, reminder):
        # A contents page should return nothing rather than be padded.
        assert "empty list" in system
        assert '{"questions": []}' in reminder

    def test_the_question_count_is_a_range_not_a_target(self, system):
        assert "Do not pad" in system
        assert "between 2 and 8" in system
