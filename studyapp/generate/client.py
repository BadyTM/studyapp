"""Talking to Claude, two ways, behind one interface.

Either an API key is paying (`ApiBackend`) or a Claude Code subscription is
(`ClaudeCodeBackend`). Both take a system prompt and a passage and return the
same list of raw question dictionaries; validate.py cannot tell them apart, and
neither can the worker.

The difference that does not go away: the CLI cannot be given a custom tool, so
the subscription path asks for JSON and parses it, while the API path uses a real
tool schema with `tool_choice`. Everything the parsing might get wrong is caught
by validation afterwards, so a mangled reply costs a chunk, never a bad question.
"""

import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .. import config
from . import prompts
from .prompts import QUESTION_TOOL

# The SDK is imported on first use, not here. At module scope it cost two
# seconds of every start -- on the path the app spends 99% of its life, where
# nothing generates anything. `encode.py` keeps PyTorch out of the app the same
# way; this is the same rule applied to a smaller dependency.
_NOT_LOOKED_FOR = "not looked for yet"
_SDK = _NOT_LOOKED_FOR


def sdk():
    """The anthropic package, or None. A subscription needs no SDK at all."""
    global _SDK
    if _SDK is _NOT_LOOKED_FOR:
        try:
            import anthropic
        except ImportError:
            _SDK = None
        else:
            _SDK = anthropic
    return _SDK


def api_available() -> bool:
    return sdk() is not None
MISSING_PACKAGE = "The anthropic package is not installed:  pip install anthropic"

# Retried: rate limiting, and the transient server-side failures. Anything else
# is a bug or a bad request and retrying it just wastes time and money.
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
MAX_ATTEMPTS = 5
BASE_DELAY = 2.0
MAX_DELAY = 60.0

# Prices per million tokens, Claude Opus 5.
PRICE_INPUT = 5.00
PRICE_OUTPUT = 25.00
PRICE_CACHE_WRITE = 6.25
PRICE_CACHE_READ = 0.50


class GenerationError(RuntimeError):
    """A chunk could not be generated. Carries a line fit to show the user.

    `retryable` exists because the subscription path has no status codes to read.
    Where the API says 429 and means "again in a moment", the CLI says only that
    it exited non-zero, so whoever raises has to say whether it is worth another
    attempt.
    """

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


# --- cost tracking ----------------------------------------------------------


class Costs:
    """Tokens spent, and what they cost.

    Wanted from the first run, not the tenth: tuning the prompt means generating
    the same five chunks over and over, and it is worth seeing that add up.
    """

    def __init__(self):
        self.input = self.output = self.cache_write = self.cache_read = 0
        self.calls = 0
        # Several workers share one backend and so one counter. `+=` is not
        # atomic, and a lost token count is the kind of bug nobody ever notices
        # is a bug -- the number is just quietly a little wrong.
        self._lock = threading.Lock()

    def add(self, usage) -> None:
        """Adds one reply's usage, from an SDK object or the CLI's JSON.

        `or default` on both paths, not just the attribute one. A dict whose
        key is present and null -- which is how the CLI reports a reply that
        touched no cache -- gave `dict.get(key, 0)` a None to return, and the
        next `+=` raised. After the call had been made and paid for, losing the
        chunk it was for.
        """
        if isinstance(usage, dict):
            def get(key, default=0):
                return usage.get(key) or default
        else:
            def get(key, default=0):
                return getattr(usage, key, default) or default
        with self._lock:
            self.calls += 1
            self.input += get("input_tokens", 0)
            self.output += get("output_tokens", 0)
            self.cache_write += get("cache_creation_input_tokens", 0)
            self.cache_read += get("cache_read_input_tokens", 0)

    @property
    def dollars(self) -> float:
        return (self.input * PRICE_INPUT
                + self.output * PRICE_OUTPUT
                + self.cache_write * PRICE_CACHE_WRITE
                + self.cache_read * PRICE_CACHE_READ) / 1_000_000

    def status_text(self) -> str:
        return f"{self.calls} calls · {self.input + self.output} tokens · ${self.dollars:.2f}"


class SubscriptionCosts(Costs):
    """The same counters, without the dollars -- a subscription is not metered."""

    def status_text(self) -> str:
        return f"{self.calls} calls · {self.input + self.output} tokens · on subscription"

    @property
    def dollars(self) -> float:
        return 0.0


# --- retrying ---------------------------------------------------------------

def retry_delay(attempt: int, rng: random.Random | None = None) -> float:
    """Exponential backoff with jitter.

    The jitter is not a nicety. Four workers hit the same rate limit within
    milliseconds of each other, and without a random spread all four then retry
    in the same instant, get limited again, and keep step for all five attempts.
    """
    ceiling = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
    return (rng or random).uniform(ceiling / 2, ceiling)


def _should_retry(error: Exception) -> bool:
    if isinstance(error, GenerationError):
        return error.retryable
    status = getattr(error, "status_code", None)
    if status is not None:
        return status in RETRY_STATUS
    package = sdk()
    if package is not None and isinstance(error, package.APIConnectionError):
        return True
    return isinstance(error, (TimeoutError, ConnectionError, subprocess.TimeoutExpired))


def with_retries(call, is_cancelled=None, sleep=time.sleep):
    """Runs `call`, retrying the failures that are worth retrying.

    Cancellation is checked between attempts only. A request already in flight is
    left to finish -- it has been paid for either way, and tearing it down mid
    stream buys nothing.
    """
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        if is_cancelled and is_cancelled():
            raise GenerationError("Cancelled.")
        try:
            return call()
        except Exception as error:      # noqa: BLE001 - re-raised below
            last = error
            if not _should_retry(error) or attempt == MAX_ATTEMPTS - 1:
                raise
            sleep(retry_delay(attempt))
    raise last                          # unreachable, kept for the type checker


# --- parsing ----------------------------------------------------------------

FENCED_JSON = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)


def extract_questions(text: str) -> list[dict]:
    """Pulls the question list out of a plain-text reply.

    Only the subscription path needs this -- the API path gets a filled-in tool
    schema. Tolerant on purpose: a stray sentence before the JSON is common and
    is not a reason to lose the chunk.
    """
    candidates = [match.group(1) for match in FENCED_JSON.finditer(text)]
    start = text.find("{")
    if start >= 0:
        candidates.append(text[start:text.rfind("}") + 1])
    start = text.find("[")
    if start >= 0:
        candidates.append(text[start:text.rfind("]") + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            parsed = parsed.get("questions")
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    raise GenerationError("No question list found in the reply.")


def describe_error(error: Exception) -> str:
    """One line about a failure, fit to put in `chunks.error` and show in a table."""
    package = sdk()
    if package is not None and isinstance(error, package.APIStatusError):
        return f"API {error.status_code}: {error.message}"
    if package is not None and isinstance(error, package.APIConnectionError):
        return "Connection failed."
    return f"{type(error).__name__}: {error}"


# --- the API path -----------------------------------------------------------


class ApiBackend:
    """Generation paid for with API credit, answering through the tool schema."""

    name = "api"
    output_instruction = prompts.TOOL_OUTPUT

    def __init__(self, client=None):
        package = sdk()
        if package is None:
            raise GenerationError(MISSING_PACKAGE)
        key = config.api_key()
        if not key and not os.environ.get("ANTHROPIC_API_KEY"):
            raise GenerationError(
                "No API key. Store one with:\n"
                "  python -c \"import keyring; keyring.set_password("
                "'studyapp', 'anthropic', 'sk-ant-...')\""
            )
        self.client = client or package.Anthropic(api_key=key)
        self.costs = Costs()

    def generate(self, system: str, user: str, is_cancelled=None) -> list[dict]:
        def call():
            return self.client.messages.create(
                model=config.MODEL,
                max_tokens=config.MAX_TOKENS,
                system=system,
                tools=[QUESTION_TOOL],
                tool_choice={"type": "tool", "name": QUESTION_TOOL["name"]},
                messages=[{"role": "user", "content": user}],
            )

        response = with_retries(call, is_cancelled)
        self.costs.add(response.usage)

        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                questions = block.input.get("questions")
                return questions if isinstance(questions, list) else []
        # tool_choice was forced, so this means the model refused or ran out of
        # room -- either way there is nothing to salvage from the reply.
        raise GenerationError(f"No tool call in the reply (stop: {response.stop_reason}).")


# --- the subscription path --------------------------------------------------

# Claude Code has no use for its tools here and would only stop to ask for
# permission to use them.
DISABLED_TOOLS = ["Bash", "Edit", "Write", "Read", "Glob", "Grep",
                  "WebSearch", "WebFetch", "Task", "NotebookEdit", "TodoWrite"]

NOT_AN_ASSISTANT = """

You are not a coding assistant and you have no tools. Never mention files, \
projects, repositories or this session, and never offer to read or save anything. \
Everything you need is in the message."""

CLI_TIMEOUT = 300


def find_claude() -> str | None:
    """The Claude Code executable, or None."""
    for name in ("claude.cmd", "claude.exe", "claude"):
        found = shutil.which(name)
        if found:
            return found
    npm_default = os.path.expandvars(r"%APPDATA%\npm\claude.cmd")
    return npm_default if os.path.exists(npm_default) else None


def session_dir() -> Path:
    """An empty but stable folder for the CLI to run in.

    Started inside the project it would read the repository as context and answer
    like a coding assistant; started in a fresh temp folder every time it would
    throw away its own session state.
    """
    path = config.data_dir() / "claude-session"
    path.mkdir(parents=True, exist_ok=True)
    return path


class ClaudeCodeBackend:
    """Generation paid for with a subscription, through the Claude Code CLI.

    One process per chunk. There is no conversation to keep -- each chunk is
    independent -- so nothing is gained by holding a process open between them.
    """

    name = "claude-code"
    output_instruction = prompts.JSON_OUTPUT

    def __init__(self, executable: str | None = None):
        self.executable = executable or find_claude()
        if not self.executable:
            raise GenerationError(
                "Claude Code not found. Install it with:  "
                "npm install -g @anthropic-ai/claude-code"
            )
        self.workdir = session_dir()
        self.costs = SubscriptionCosts()

    def command(self, system: str) -> list[str]:
        return [
            self.executable, "-p",
            "--output-format", "json",
            "--system-prompt", system + NOT_AN_ASSISTANT,
            "--disallowedTools", *DISABLED_TOOLS,
        ]

    def generate(self, system: str, user: str, is_cancelled=None) -> list[dict]:
        command = self.command(system)
        user = user + prompts.json_reminder(config.MIN_QUESTIONS_PER_CHUNK, config.MAX_QUESTIONS_PER_CHUNK)

        def call():
            # The passage goes down the pipe, never on the command line: Windows
            # stops at 8191 characters and a chunk is comfortably longer than that.
            completed = subprocess.run(
                command,
                input=user,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.workdir,
                timeout=CLI_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0:
                # Retried, unlike the errors the CLI reports in its own JSON.
                # This is the process itself failing, which is where a dropped
                # connection or a rate limit on the CLI's own request surfaces,
                # and nothing here can tell those from a permanent fault. Five
                # attempts with backoff costs seconds; not retrying costs the
                # chunk. A CLI that is missing or broken never reaches this --
                # find_claude() fails first.
                raise GenerationError(
                    (completed.stderr or "").strip()[:300]
                    or f"Claude Code exited with {completed.returncode}.",
                    retryable=True,
                )
            return completed.stdout

        stdout = with_retries(call, is_cancelled)

        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError:
            raise GenerationError("Claude Code returned something that is not JSON.")

        if envelope.get("is_error"):
            raise GenerationError(str(envelope.get("result"))[:300])
        self.costs.add(envelope.get("usage") or {})
        return extract_questions(str(envelope.get("result", "")))


# --- choosing ---------------------------------------------------------------


def create_backend(preference: str | None = None):
    """The backend to generate with.

    `auto` spends the subscription when the CLI is installed and falls back to an
    API key otherwise -- the same order as the-narrator, and for the same reason:
    a subscription is already paid for.
    """
    preference = preference or config.BACKEND

    if preference == "api":
        return ApiBackend()
    if preference == "claude-code":
        return ClaudeCodeBackend()

    if find_claude():
        return ClaudeCodeBackend()
    if api_available() and config.api_key():
        return ApiBackend()
    raise GenerationError(
        "No way to reach Claude. Either install Claude Code "
        "(npm install -g @anthropic-ai/claude-code) or store an API key in the keyring."
    )
