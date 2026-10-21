#
#  provenance.py
#
#  Recording how a snapshot was made, in its `produced_by` metadata.
#
#  The descriptive fields of a snapshot (source_name, access_notes, ...) are
#  written by hand, describe the dataset, and carry over to its next version.
#  `produced_by` is written by alcove, describes one version, and never carries
#  over: the script or command that made the data, its arguments, the commit
#  it ran at, and when.
#

import json
import os
import re
import socket
import subprocess
import sys
import threading
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import IO, Any, Optional

from alcove.paths import BASE_DIR

REDACTED = "REDACTED"

# how much of a captured command's stderr to keep
STDERR_TAIL_CHARS = 2000

# how long to keep reading stderr once the command has exited; a background
# process it started can hold the pipe open indefinitely
STDERR_DRAIN_SECONDS = 2

# words that mark a secret, matched as a whole segment of a name, so that
# `--api-token` and `GITHUB_TOKEN` count but `max_tokens` and `--author` don't
_SECRET_WORD = r"(?:token|passw(?:or)?d|secrets?|api[-_]?key|credentials?|auth)"
_SEGMENTED = rf"(?:[a-z0-9]+[-_])*{_SECRET_WORD}(?:[-_][a-z0-9]+)*"

# a flag whose value is likely a secret, e.g. --token, --api-key, --db-password
_SECRET_FLAG = re.compile(rf"^{_SEGMENTED}$", re.IGNORECASE)
# flags that look like the above but take no value, e.g. --password-stdin
_VALUELESS_FLAG = re.compile(r"^no[-_]|[-_]stdin$|^(use|skip)[-_]", re.IGNORECASE)
# the same, written inside a shell script, e.g. `sh -c 'mycli --token abc | jq'`
_INLINE_FLAG = re.compile(
    rf"(?<!\S)(--?({_SEGMENTED}))(=|\s+)(?!-)('[^']*'|\"[^\"]*\"|[^\s;|&)]+)",
    re.IGNORECASE,
)
# an environment-style assignment, e.g. `env GITHUB_TOKEN=... cmd`; a whole
# argument that is one keeps everything after the =, spaces and all
_SECRET_ASSIGNMENT = re.compile(
    r"(?<![\w-])((?:[a-z0-9]+_)*(?:token|passw(?:or)?d|secrets?|api_?key|credentials?)"
    r"(?:_[a-z0-9]+)*=)('[^']*'|\"[^\"]*\"|[^\s;|&)]+)",
    re.IGNORECASE,
)
_WHOLE_ASSIGNMENT = re.compile(
    r"^((?:[a-z0-9]+_)*(?:token|passw(?:or)?d|secrets?|api_?key|credentials?)"
    r"(?:_[a-z0-9]+)*=).+$",
    re.IGNORECASE | re.DOTALL,
)
# a header whose value is likely a secret, e.g. Authorization, X-Auth-Token;
# the value runs to the end of the line, or of the quotes it is in
_SECRET_HEADER = re.compile(
    r"(?<![\w-])(authorization|proxy-authorization|cookie|set-cookie|"
    r"(?:[a-z0-9]+-)*(?:token|api-key|secret)(?:-[a-z0-9]+)*)(\s*:[ \t]*)[^\s'\"][^\n'\"]*",
    re.IGNORECASE,
)
# credentials in a URL: https://user:pass@host or https://TOKEN@host
_URL_USERINFO = re.compile(r"(\w+://)([^/?#\s@]+)@")
# a query parameter whose value is likely a secret, e.g. ?token=, X-Amz-Signature=
_SECRET_QUERY = re.compile(
    r"([?&](?:(?:[a-z0-9]+[-_.])*(?:token|secret|signature|credential|password|api_?key|sig)"
    r"(?:[-_.][a-z0-9]+)*|key)=)[^&\s'\"#]+",
    re.IGNORECASE,
)

# shells, whose `-c` argument is a script rather than one value
_SCRIPT_FLAG = re.compile(r"^-[a-z]*c$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact(argv: list[str]) -> list[str]:
    """Hide the likely secrets in a command line, and write the home directory as ~.

    Best effort: it catches the common shapes (--token X, --api-key=X,
    TOKEN=X, Authorization and *-Token headers, credentials in URLs, and
    token, key or signature query parameters), including inside a shell
    script given as one argument, but not a secret passed any other way.
    Text is changed only where something is hidden, so a script still reads
    as it was written.
    """
    redacted = []
    hide_next = False
    for i, arg in enumerate(argv):
        after_c = i > 0 and bool(_SCRIPT_FLAG.match(argv[i - 1]))
        if hide_next:
            hide_next = False
            if not arg.startswith("-"):
                redacted.append(REDACTED)
                continue

        if arg.startswith("-"):
            flag, eq, _ = arg.partition("=")
            name = flag.lstrip("-")
            if _SECRET_FLAG.search(name) and not _VALUELESS_FLAG.search(name):
                if eq:
                    redacted.append(f"{flag}={REDACTED}")
                else:
                    redacted.append(arg)
                    hide_next = True
                continue

        if not after_c and _WHOLE_ASSIGNMENT.match(arg):
            redacted.append(_WHOLE_ASSIGNMENT.sub(rf"\1{REDACTED}", _rewrite_home(arg)))
            continue

        redacted.append(redact_text(arg))

    return redacted


def redact_text(text: str) -> str:
    "Hide likely secrets in free text, such as a shell script or a command's stderr."
    text = _rewrite_home(text)
    text = _INLINE_FLAG.sub(_hide_inline_flag, text)
    text = _SECRET_ASSIGNMENT.sub(rf"\1{REDACTED}", text)
    text = _SECRET_HEADER.sub(rf"\1\2{REDACTED}", text)
    text = _URL_USERINFO.sub(rf"\1{REDACTED}@", text)
    text = _SECRET_QUERY.sub(rf"\1{REDACTED}", text)
    return text


def _hide_inline_flag(m: re.Match) -> str:
    if _VALUELESS_FLAG.search(m.group(2)):
        return m.group(0)
    return f"{m.group(1)}{m.group(3)}{REDACTED}"


def _rewrite_home(text: str) -> str:
    "Metadata is often committed and pushed, so don't name the user's home."
    home = os.path.expanduser("~").rstrip("/")
    if not home or home not in text:
        return text
    return re.sub(rf"(?<![\w.-]){re.escape(home)}(?=/|\s|$|['\";:])", "~", text)


def describe_caller(extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """A `produced_by` record for a snapshot being made now, by this process.

    Run from a Python script, it names the script, the commit it ran at and
    whether it had uncommitted changes. Keys in `extra` are added, replacing
    any of the same name; their values must be plain data (strings, numbers,
    booleans, lists and dicts of them), since the record is saved as YAML.
    """
    if extra:
        extra = _plain_data(extra)

    record: dict[str, Any] = {}

    script = _calling_script()
    if script is not None:
        record["command"] = redact([_display_path(script), *sys.argv[1:]])
        record["script"] = _display_path(script)
        commit, changed = _git_state(script)
        if commit is not None:
            record["git_commit"] = commit
            record["script_changed"] = changed
    else:
        program = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "python"
        record["command"] = redact([program, *sys.argv[1:]])

    if extra:
        record.update(extra)

    # last, after what the caller says about the command
    record.setdefault("snapshotted_at", utc_now())
    record.setdefault("host", socket.gethostname())
    record.setdefault("alcove_version", _alcove_version())

    return record


def run_and_capture(
    command: list[str], stdout: Optional[IO[bytes]], cwd: Optional[Path] = None
) -> tuple[int, str]:
    """Run a command, passing its stderr through to ours as it goes.

    Returns its exit code and the end of what it wrote to stderr, with likely
    secrets hidden. Its stdout goes to `stdout`, or to ours if that is None.
    """
    try:
        proc = subprocess.Popen(command, stdout=stdout, stderr=subprocess.PIPE, cwd=cwd)
    except FileNotFoundError:
        raise CommandNotFound(command[0])
    except PermissionError:
        raise CommandNotFound(f"{command[0]} (not executable)")

    assert proc.stderr is not None
    stderr_pipe = proc.stderr
    tail = bytearray()

    def pump() -> None:
        try:
            _pump()
        except (ValueError, OSError):
            # the pipe was closed under us, after the drain timeout
            pass

    def _pump() -> None:
        # e.g. under Jupyter, sys.stderr is text only
        out = getattr(sys.stderr, "buffer", None)
        for chunk in iter(lambda: stderr_pipe.read1(8192), b""):  # type: ignore[attr-defined]
            # keep draining whatever happens to the pass-through, or the
            # command blocks once the pipe fills
            try:
                if out is not None:
                    out.write(chunk)
                    out.flush()
                else:
                    sys.stderr.write(chunk.decode("utf-8", errors="replace"))
            except Exception:
                pass
            tail.extend(chunk)
            # keep a little more than we need, so a cut doesn't split a character
            if len(tail) > 4 * STDERR_TAIL_CHARS:
                del tail[: len(tail) - 4 * STDERR_TAIL_CHARS]

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        returncode = proc.wait()
    except BaseException:
        # e.g. Ctrl-C, which the command has also been sent
        proc.kill()
        proc.wait()
        raise
    finally:
        reader.join(timeout=STDERR_DRAIN_SECONDS)
        if reader.is_alive():
            # a process the command started still holds the pipe; stop
            # passing its output through
            try:
                stderr_pipe.close()
            except Exception:
                pass

    # redact before cutting, so a cut through a secret can't hide it from the rules
    stderr = redact_text(tail.decode("utf-8", errors="replace")).strip()
    if len(stderr) > STDERR_TAIL_CHARS:
        stderr = stderr[-STDERR_TAIL_CHARS:]
        # start at a whole line
        stderr = stderr.partition("\n")[2] or stderr
    return returncode, stderr


class CommandNotFound(Exception):
    "The command to capture can't be run."


def _plain_data(extra: dict[str, Any]) -> dict[str, Any]:
    """`extra` as plain JSON-like values, e.g. tuples as lists and an IntEnum as
    an int, which YAML can save without Python-only tags."""
    try:
        return json.loads(json.dumps(extra))
    except (TypeError, ValueError) as e:
        raise TypeError(
            f"produced_by values must be plain data (strings, numbers, booleans, "
            f"lists and dicts): {e}"
        ) from None


def _calling_script() -> Optional[Path]:
    "The Python script this process is running, if it is one of the user's."
    if not sys.argv or not sys.argv[0]:
        return None
    path = Path(sys.argv[0])
    if path.suffix != ".py" or not path.is_file():
        return None
    path = path.resolve()

    # an installed package's launcher, e.g. Jupyter's or a `python -m`
    # package's __main__.py, is not the script that made the data
    if {"site-packages", "dist-packages"} & set(path.parts):
        return None

    return path


def _display_path(path: Path) -> str:
    "A path relative to the alcove when it's inside it, otherwise from ~ if it can be."
    try:
        return str(path.relative_to(BASE_DIR.resolve()))
    except ValueError:
        pass
    try:
        home = Path(os.path.expanduser("~")).resolve()
        return str(Path("~") / path.relative_to(home))
    except ValueError:
        return str(path)


def _git_state(script: Path) -> tuple[Optional[str], bool]:
    """The commit checked out where the script lives, and whether the script
    differs from it. A script git doesn't track counts as changed."""
    git = ["git", "-C", str(script.parent)]
    try:
        commit = subprocess.run(
            [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        # not in a git repo, a repo with no commits yet, or no git at all
        return None, False

    tracked = (
        subprocess.run(
            [*git, "ls-files", "--error-unmatch", "--", script.name],
            capture_output=True,
        ).returncode
        == 0
    )
    if not tracked:
        # untracked or ignored: the commit doesn't have it
        return commit, True

    status = subprocess.run(
        [*git, "status", "--porcelain", "--", script.name],
        capture_output=True,
        text=True,
    ).stdout.strip()
    return commit, bool(status)


def _alcove_version() -> str:
    try:
        return version("alcove")
    except PackageNotFoundError:
        return "unknown"
