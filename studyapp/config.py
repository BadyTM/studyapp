"""Paths, constants and credentials.

Everything that depends on where the app stores its data or how the API is paid
for lives here, so the rest of the code stays ignorant of both.
"""

import os
import sys
from pathlib import Path

APP_NAME = "StudyApp"
ORG_NAME = "StudyApp"

# Windows groups taskbar buttons by this, not by the executable. Without it the
# window is filed under pythonw.exe and shows Python's icon there whatever the
# window icon says.
APP_ID = "StudyApp.StudyApp"

# --- paths ------------------------------------------------------------------


def data_dir() -> Path:
    """Where the database and the render cache live.

    Deliberately outside the project folder: the question bank is personal data
    that survives reinstalling the app, and OneDrive syncing a WAL-mode SQLite
    file mid-write is a good way to corrupt it.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def assets_dir() -> Path:
    """Where the icon lives. Next to the package, not in the data directory."""
    return Path(__file__).resolve().parent.parent / "assets"


def icon_path() -> Path:
    return assets_dir() / "studyapp.ico"


def db_path() -> Path:
    return Path(os.environ.get("STUDYAPP_DB") or data_dir() / "studyapp.db")


def blur_cache_dir(document_sha: str) -> Path:
    """Pre-rendered blurred pages for one document, keyed by its hash.

    Keyed by hash rather than filename so re-importing an edited PDF cannot show
    the previous edition's pages behind the blur.
    """
    path = data_dir() / "cache" / "blur" / document_sha[:16]
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- generation -------------------------------------------------------------

MODEL = "claude-opus-5"
MAX_TOKENS = 8000

# Questions are written in the language of the source PDF, detected at ingest and
# stored on the document. The UI stays English regardless; only the question text,
# options and explanations follow the source. `source_quote` is always verbatim.
QUESTION_LANGUAGE = "source"

MIN_QUESTIONS_PER_CHUNK = 2
MAX_QUESTIONS_PER_CHUNK = 8

# How many chunks are in flight at once. Above ~5 the API starts returning 429 and
# the retry backoff eats the gain.
MAX_PARALLEL_CHUNKS = 4

# Backend for the generation step: "auto" prefers a Claude Code subscription and
# falls back to an API key, "api" and "claude-code" force one of them.
BACKEND = os.environ.get("STUDYAPP_BACKEND", "auto")

KEYRING_SERVICE = "studyapp"
KEYRING_USER = "anthropic"


def api_key() -> str | None:
    """The Anthropic API key, from the OS credential store or the environment.

    Never QSettings: that is a plaintext INI file in the user's profile.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:
        # A missing or locked keyring backend closes this path; it is not a crash.
        return None


# --- chunking ---------------------------------------------------------------

CHUNK_MIN_WORDS = 1500
CHUNK_MAX_WORDS = 2500
