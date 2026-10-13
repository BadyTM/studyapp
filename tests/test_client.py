"""Retrying, parsing and cost counting -- everything about the backends that can
be checked without spending anything.

The retry rules matter more than they look. Five workers that all back off by the
same amount retry in the same instant, get rate-limited together, and stay in
step for all five attempts; the jitter is what breaks that up.
"""

import random
from pathlib import Path

import pytest

from studyapp.generate import client
from studyapp.generate.client import Costs, GenerationError, SubscriptionCosts

ROOT = Path(__file__).resolve().parent.parent


class FakeStatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class TestRetryDelay:
    def test_grows_with_each_attempt(self):
        rng = random.Random(0)
        early = client.retry_delay(0, rng)
        late = client.retry_delay(4, rng)
        assert late > early

    def test_is_capped(self):
        assert client.retry_delay(20, random.Random(0)) <= client.MAX_DELAY

    def test_two_workers_do_not_retry_in_the_same_instant(self):
        delays = {client.retry_delay(2, random.Random(seed)) for seed in range(8)}
        assert len(delays) > 1, "no jitter: every worker would retry together"

    def test_never_negative(self):
        assert all(client.retry_delay(n, random.Random(n)) >= 0 for n in range(6))


class TestWithRetries:
    def test_returns_on_first_success(self):
        assert client.with_retries(lambda: "ok", sleep=lambda _: None) == "ok"

    def test_retries_a_rate_limit_then_succeeds(self):
        attempts = []

        def call():
            attempts.append(1)
            if len(attempts) < 3:
                raise FakeStatusError(429)
            return "ok"

        assert client.with_retries(call, sleep=lambda _: None) == "ok"
        assert len(attempts) == 3

    def test_gives_up_after_the_maximum(self):
        attempts = []

        def call():
            attempts.append(1)
            raise FakeStatusError(529)

        with pytest.raises(FakeStatusError):
            client.with_retries(call, sleep=lambda _: None)
        assert len(attempts) == client.MAX_ATTEMPTS

    def test_does_not_retry_a_bad_request(self):
        # 400 means the request itself is wrong; sending it again five times
        # wastes time and, on the API path, money.
        attempts = []

        def call():
            attempts.append(1)
            raise FakeStatusError(400)

        with pytest.raises(FakeStatusError):
            client.with_retries(call, sleep=lambda _: None)
        assert len(attempts) == 1

    def test_does_not_retry_a_programming_error(self):
        attempts = []

        def call():
            attempts.append(1)
            raise ValueError("bug")

        with pytest.raises(ValueError):
            client.with_retries(call, sleep=lambda _: None)
        assert len(attempts) == 1

    def test_cancellation_is_checked_between_attempts(self):
        attempts = []

        def call():
            attempts.append(1)
            raise FakeStatusError(429)

        with pytest.raises(GenerationError):
            client.with_retries(call, is_cancelled=lambda: len(attempts) >= 2,
                                sleep=lambda _: None)
        assert len(attempts) == 2, "a request already in flight is left to finish"

    def test_a_connection_drop_is_retried(self):
        attempts = []

        def call():
            attempts.append(1)
            if len(attempts) < 2:
                raise ConnectionError("reset")
            return "ok"

        assert client.with_retries(call, sleep=lambda _: None) == "ok"


class TestExtractQuestions:
    def test_bare_object(self):
        assert client.extract_questions('{"questions": [{"type": "open"}]}') == [
            {"type": "open"}]

    def test_fenced_block(self):
        text = 'Tady jsou otázky:\n```json\n{"questions": [{"type": "mcq"}]}\n```\n'
        assert client.extract_questions(text) == [{"type": "mcq"}]

    def test_preamble_before_the_json(self):
        # Common on the subscription path and not worth losing a chunk over.
        text = 'Prošel jsem text a napsal tři otázky.\n{"questions": [{"type": "open"}]}'
        assert client.extract_questions(text) == [{"type": "open"}]

    def test_a_bare_array(self):
        assert client.extract_questions('[{"type": "open"}]') == [{"type": "open"}]

    def test_an_empty_list_is_a_valid_answer(self):
        # A contents page warrants no questions; that is not a parse failure.
        assert client.extract_questions('{"questions": []}') == []

    def test_non_dict_entries_are_dropped(self):
        assert client.extract_questions('{"questions": [{"type": "open"}, "junk"]}') == [
            {"type": "open"}]

    def test_nothing_parseable_raises(self):
        with pytest.raises(GenerationError):
            client.extract_questions("Omlouvám se, ale tomu textu nerozumím.")

    def test_empty_reply_raises(self):
        with pytest.raises(GenerationError):
            client.extract_questions("")


class TestCosts:
    def test_dollars_follow_the_price_list(self):
        costs = Costs()
        costs.add({"input_tokens": 1_000_000, "output_tokens": 0})
        assert costs.dollars == pytest.approx(client.PRICE_INPUT)

    def test_accepts_an_sdk_usage_object(self):
        class Usage:
            input_tokens = 100
            output_tokens = 50
            cache_creation_input_tokens = None
            cache_read_input_tokens = None

        costs = Costs()
        costs.add(Usage())
        assert costs.input == 100 and costs.output == 50
        assert costs.cache_write == 0, "None must not leak into the arithmetic"

    def test_calls_are_counted(self):
        costs = Costs()
        for _ in range(3):
            costs.add({})
        assert costs.calls == 3
        assert "3 calls" in costs.status_text()

    def test_a_subscription_is_not_priced_in_dollars(self):
        costs = SubscriptionCosts()
        costs.add({"input_tokens": 1_000_000, "output_tokens": 1_000_000})
        assert costs.dollars == 0.0
        assert "subscription" in costs.status_text()


class TestBackendSelection:
    def test_auto_prefers_the_subscription(self, monkeypatch):
        monkeypatch.setattr(client, "find_claude", lambda: "C:/fake/claude.cmd")
        assert client.create_backend("auto").name == "claude-code"

    def test_auto_falls_back_to_the_api(self, monkeypatch):
        monkeypatch.setattr(client, "find_claude", lambda: None)
        monkeypatch.setattr(client.config, "api_key", lambda: "sk-ant-test")
        if not client.api_available():
            pytest.skip("anthropic not installed")
        assert client.create_backend("auto").name == "api"

    def test_neither_available_says_so(self, monkeypatch):
        monkeypatch.setattr(client, "find_claude", lambda: None)
        monkeypatch.setattr(client.config, "api_key", lambda: None)
        with pytest.raises(GenerationError, match="No way to reach Claude"):
            client.create_backend("auto")

    def test_forcing_a_missing_cli_is_an_error(self, monkeypatch):
        monkeypatch.setattr(client, "find_claude", lambda: None)
        with pytest.raises(GenerationError, match="Claude Code not found"):
            client.create_backend("claude-code")


class TestDescribeError:
    def test_a_plain_exception(self):
        assert client.describe_error(ValueError("boom")) == "ValueError: boom"

    def test_stays_on_one_line(self):
        message = client.describe_error(RuntimeError("first\nsecond"))
        assert isinstance(message, str)


class TestClaudeCodeCommand:
    def test_the_passage_never_goes_on_the_command_line(self, monkeypatch):
        # Windows cuts argv off at 8191 characters and a chunk is longer than
        # that. Only the system prompt is passed as an argument.
        monkeypatch.setattr(client, "find_claude", lambda: "C:/fake/claude.cmd")
        backend = client.ClaudeCodeBackend()
        command = backend.command("SYSTEM PROMPT")
        assert sum(len(part) for part in command) < 7500
        assert "--output-format" in command and "json" in command

    def test_tools_are_disabled(self, monkeypatch):
        monkeypatch.setattr(client, "find_claude", lambda: "C:/fake/claude.cmd")
        command = client.ClaudeCodeBackend().command("x")
        assert "--disallowedTools" in command
        assert "Read" in command and "Bash" in command


class TestUsageThatCarriesNulls:
    """The CLI reports a reply that touched no cache as `null`, not as 0.

    `dict.get(key, 0)` hands back the None that is actually there, and the next
    `+=` raises -- after the call has been made and paid for, losing the chunk
    it was for.
    """

    def test_null_cache_fields(self):
        costs = client.Costs()
        costs.add({"input_tokens": 10, "output_tokens": 5,
                   "cache_creation_input_tokens": None,
                   "cache_read_input_tokens": None})
        assert (costs.input, costs.output) == (10, 5)
        assert (costs.cache_write, costs.cache_read) == (0, 0)

    def test_null_everywhere(self):
        costs = client.Costs()
        costs.add({"input_tokens": None, "output_tokens": None,
                   "cache_creation_input_tokens": None,
                   "cache_read_input_tokens": None})
        assert costs.dollars == 0.0
        assert costs.calls == 1

    def test_missing_keys_entirely(self):
        costs = client.Costs()
        costs.add({})
        assert costs.calls == 1 and costs.input == 0

    def test_an_sdk_object_with_nulls(self):
        usage = type("Usage", (), {
            "input_tokens": 7, "output_tokens": 3,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None})()
        costs = client.Costs()
        costs.add(usage)
        assert (costs.input, costs.cache_read) == (7, 0)

    def test_a_subscription_counter_takes_them_too(self):
        costs = client.SubscriptionCosts()
        costs.add({"input_tokens": None, "output_tokens": 4,
                   "cache_read_input_tokens": None})
        assert costs.output == 4


class TestTheSdkIsNotImportedUntilItIsNeeded:
    def test_importing_the_client_does_not_import_anthropic(self):
        # Two seconds of every start, on the path that never generates anything.
        # The same rule `encode.py` uses to keep PyTorch out of the app.
        import subprocess
        import sys

        probe = subprocess.run(
            [sys.executable, "-c",
             "import sys; from studyapp.generate import client; "
             "print('anthropic' in sys.modules)"],
            capture_output=True, text=True, cwd=str(ROOT))
        assert probe.stdout.strip() == "False", probe.stderr

    def test_asking_for_it_still_works(self):
        assert client.api_available() is (client.sdk() is not None)
