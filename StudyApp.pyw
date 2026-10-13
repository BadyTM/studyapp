"""Double-click this to open StudyApp, with no console window behind it.

The code lives in the studyapp/ package next to this file; this is only a
launcher. The .pyw extension is what makes Windows open it with pythonw.exe
instead of python.exe, which is what keeps the console away.

The one thing this has to get right is which interpreter runs: double-clicking
hands the file to the *system* pythonw.exe, which has no PySide6 installed. So
the first thing it does is hand itself over to the virtualenv's.

Where that virtualenv lives is deliberately not fixed. PySide6 alone is some 660
megabytes, and this project sits inside OneDrive -- keeping the environment in
`.venv` here means syncing all of it, forever, for no benefit. So the launcher
also looks outside the project, and STUDYAPP_VENV overrides both.

The filename is what shows up on the desktop, which is why it reads as a name
rather than as a module path.
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def candidate_venvs() -> list[Path]:
    """Where to look for the interpreter, best first."""
    places = []
    override = os.environ.get("STUDYAPP_VENV")
    if override:
        places.append(Path(override))
    places.append(HERE / ".venv")
    # Outside the synced tree, which is where it belongs on this machine.
    local = os.environ.get("LOCALAPPDATA")
    if local:
        places.append(Path(local) / "StudyApp" / "venv")
    return places


def find_pythonw() -> Path | None:
    for venv in candidate_venvs():
        interpreter = venv / "Scripts" / "pythonw.exe"
        if interpreter.exists():
            return interpreter
    return None


def show_error(title: str, message: str) -> None:
    """A message box that works even when Qt is the thing that is missing.

    With no console there is nowhere for a traceback to go, and a launcher that
    fails by doing nothing at all is the worst way to find out the virtualenv
    was never created.
    """
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        sys.stderr.write(f"{title}\n{message}\n")


def use_the_virtualenv() -> None:
    """Re-runs this file under the virtualenv's interpreter, unless already there."""
    interpreter = find_pythonw()
    if interpreter is None:
        looked = "\n".join(f"    {path}" for path in candidate_venvs())
        show_error(
            "StudyApp",
            "No virtual environment found. Looked in:\n\n"
            f"{looked}\n\n"
            "Create one with:\n"
            "    python -m venv .venv\n"
            "    .venv\\Scripts\\python.exe -m pip install -r requirements.txt",
        )
        sys.exit(1)

    running = Path(sys.executable).resolve()
    if running == interpreter.resolve():
        return

    # execv replaces this process, so everything below carries on in the child --
    # including the error handling.
    os.execv(str(interpreter), [str(interpreter), str(Path(__file__).resolve())])


def main() -> int:
    use_the_virtualenv()
    sys.path.insert(0, str(HERE))

    try:
        from studyapp.main import main as run
    except ImportError as error:
        show_error(
            "StudyApp",
            f"A dependency is missing:\n\n{error}\n\n"
            "Install them with:\n"
            "    .venv\\Scripts\\python.exe -m pip install -r requirements.txt",
        )
        return 1

    try:
        return run([str(Path(__file__).resolve())])
    except Exception:
        import traceback

        show_error("StudyApp has crashed", traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
