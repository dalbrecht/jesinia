# SPDX-License-Identifier: AGPL-3.0-or-later
"""What review agents may ask of a PR's worktree, and how much of it they may read.

PR code is untrusted and a prompt-injection channel, so every argument an agent sends to the
repo tools is checked here before anything touches the filesystem or git:

- paths are relative, never contain ``..`` or ``.git``, and carry no control characters
  (whether they *resolve* inside the tree is checked against the filesystem by the
  application layer);
- ``repo_git`` accepts only ``log``, ``show`` and ``blame`` with an allowlist of read-only
  options, conservative revisions and worktree paths;
- reading is capped per call, and per file per review session, so one huge file can't fill
  an agent's context.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

READ_LINES_PER_CALL = 2000
READ_MAX_BYTES = 256 * 1024
FILE_LINES_PER_SESSION = 4000
GREP_MAX_MATCHES = 200
GREP_MAX_BYTES = 64 * 1024
GREP_MAX_LINE_CHARS = 500
GREP_MAX_PATTERN_CHARS = 200
# Raw `git grep` output read before filtering; past it the search reports itself truncated.
GREP_RAW_BYTES = 4 * 1024 * 1024
GIT_MAX_BYTES = 64 * 1024
GIT_MAX_LINES = 2000
LS_MAX_ENTRIES = 1000
LOG_MAX_COUNT = 1000

_MAX_PATH_CHARS = 1024
_MAX_GLOB_CHARS = 200
_MAX_ARGS = 32
_MAX_ARG_CHARS = 300
_MAX_REVISIONS = 8
_MAX_PATHS = 8
_MAX_OPTION_VALUE_CHARS = 100

# Revisions start with an alphanumeric (never an option) and use only ref-name characters
# plus the ancestry operators ~ ^ and @{...}; ``..`` ranges and ``:`` are refused separately.
_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/~^@{}+-]{0,199}")
_COUNT = re.compile(r"[1-9][0-9]{0,5}")
# `-L <start>,<end>` only: no `:funcname` regex, no `/regex/` ranges.
_LINE_SPAN = re.compile(r"([0-9]{1,9}),([0-9]{1,9})")
# An ERE interval: {n}, {n,}, {n,m} or {,m}. A "{" that isn't one is an ordinary character.
REGEX_MAX_REPEAT = 50
REGEX_MAX_ALTERNATIVES = 20
# CPU budget: each open-ended repeat multiplies the backtracking work of the next, so
# `.*.*...Q` costs roughly cubically; finite repeats cost their bound in copies.
REGEX_MAX_UNBOUNDED = 4
REGEX_MAX_BOUND_TOTAL = 100
_REGEX_METACHARACTERS = frozenset(".[]()*+?{}|^$\\")

_FLAGS: dict[str, frozenset[str]] = {
    "log": frozenset({"--oneline", "--stat", "-p", "--follow", "--name-only", "--name-status"}),
    "show": frozenset({"--oneline", "--stat", "-p", "--name-only", "--name-status"}),
    "blame": frozenset(),
}
_VALUED_OPTIONS: dict[str, frozenset[str]] = {
    "log": frozenset({"--since", "--author"}),
    "show": frozenset(),
    "blame": frozenset(),
}
_ALLOWED_HELP = {
    "log": "--oneline, --stat, -p, --follow, --name-only, --name-status, -n <count>, "
    "--since=<date>, --author=<name>",
    "show": "--oneline, --stat, -p, --name-only, --name-status",
    "blame": "-L <start>,<end>",
}


class RepoToolError(Exception):
    """Base for every refusal or failure of a repo tool; the message is shown to the agent."""


class NoReviewSessionError(RepoToolError):
    """The caller is not a review session bound to a worktree."""


class UnsafePathError(RepoToolError):
    """A path is absolute, climbs out with ``..``, names ``.git``, or resolves outside the
    worktree (for example through a symlink)."""


class RepoPathNotFoundError(RepoToolError):
    """The path does not exist, or is not the kind of entry the tool needs."""


class InvalidReadRangeError(RepoToolError):
    """``start``/``end`` do not describe a line range."""


class AutoSkippedFileError(RepoToolError):
    """The file is one the coverage rules skip (lockfile, generated, vendored, binary)."""


class AllowanceExhaustedError(RepoToolError):
    """This session has already read its per-file allowance of the file."""


class UnsafeGitArgumentError(RepoToolError):
    """A ``repo_git`` command, option or revision is outside the allowlist."""


class NotACommitError(UnsafeGitArgumentError):
    """A ``repo_git`` revision names a blob, a tree or nothing, not a commit."""


class OutsidePrHistoryError(UnsafeGitArgumentError):
    """A ``repo_git`` revision is a commit, but not an ancestor of the PR's head."""


class InvalidSearchError(RepoToolError):
    """A ``repo_grep`` pattern or glob is refused before running."""


class RepoGitError(RepoToolError):
    """git itself failed or could not be run; the cause keeps git's own explanation."""


@dataclass(frozen=True)
class CappedOutput:
    """A command's stdout, cut at a byte limit; ``truncated`` says whether it was cut.
    ``exit_code`` is git's (one of the accepted codes), or 0 when git was cut off."""

    data: bytes
    truncated: bool
    exit_code: int = 0


@dataclass(frozen=True)
class RegexDialect:
    """What the regex library behind the installed git understands beyond POSIX ERE.

    glibc (Linux) and git's bundled regex know ``\\b`` and ``\\B``; the BSD library on macOS
    knows neither, but has ``[[:<:]]`` and ``[[:>:]]`` word anchors. Found by probing git.
    """

    native_word_boundaries: bool  # \b works as written
    not_word_boundary: bool  # \B works as written


BSD_REGEX = RegexDialect(native_word_boundaries=False, not_word_boundary=False)
GNU_REGEX = RegexDialect(native_word_boundaries=True, not_word_boundary=True)


@dataclass(frozen=True)
class GitPlan:
    """A checked ``repo_git`` request, ready to become argv.

    ``blobs`` holds ``show <rev>:<path>`` requests (normalized path); ``paths`` the worktree
    paths given after ``--`` (or, for ``blame``, its one file).
    """

    cmd: str
    options: tuple[str, ...]
    revisions: tuple[str, ...]
    blobs: tuple[tuple[str, str], ...]
    paths: tuple[str, ...]


class FileLineAllowance:
    """How many lines of each file each review session may still read.

    Re-reading counts again: the cap bounds what reaches the agent's context, not which
    lines it has seen.
    """

    def __init__(self, per_file: int) -> None:
        self.per_file = per_file
        self._used: dict[str, dict[str, int]] = {}

    def tracks(self, session_id: str) -> bool:
        """Whether any read state is held for ``session_id``."""
        return session_id in self._used

    def remaining(self, session_id: str, path: str) -> int:
        return self.per_file - self._used.get(session_id, {}).get(path, 0)

    def take(self, session_id: str, path: str, lines: int) -> int:
        """Reserve up to ``lines`` lines and return how many the allowance granted.

        ``path`` is any stable key for the file (the repo tools use its inode). Callers
        reserve before they read and ``refund`` what they didn't return, so concurrent
        calls can never hand out more than the allowance.
        """
        granted = max(0, min(lines, self.remaining(session_id, path)))
        used = self._used.setdefault(session_id, {})
        used[path] = used.get(path, 0) + granted
        return granted

    def refund(self, session_id: str, path: str, lines: int) -> None:
        """Give back reserved lines that were not returned to the agent."""
        used = self._used.get(session_id, {})
        if path in used:
            used[path] -= max(0, min(lines, used[path]))

    def drop(self, session_id: str) -> None:
        """Forget a session (it ended or was unbound)."""
        self._used.pop(session_id, None)


def _has_control_characters(text: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in text)


def normalize_relative_path(path: str) -> str:
    """``path`` as a clean ``/``-separated path relative to the worktree (``.`` for the root).

    Raises:
        UnsafePathError: empty, too long, absolute, containing ``..``, a ``.git`` component
            (any case) or control characters.
    """
    if not path or len(path) > _MAX_PATH_CHARS or _has_control_characters(path):
        raise UnsafePathError(f"not a usable path: {path!r}")
    if path.startswith("/"):
        raise UnsafePathError(f"absolute paths are refused; give a path inside the PR: {path!r}")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise UnsafePathError(f"'..' is refused; give a path inside the PR: {path!r}")
    if any(part.lower() == ".git" for part in parts):
        raise UnsafePathError(f"git's own files are not part of the PR: {path!r}")
    return "/".join(parts) or "."


def check_revision(revision: str) -> str:
    """Return ``revision`` if it is a plain revision (``HEAD~2``, ``main``, a SHA).

    Raises:
        UnsafeGitArgumentError: options, ranges (``..``), ``rev:path`` and odd characters.
    """
    if not _REVISION.fullmatch(revision) or ".." in revision:
        raise UnsafeGitArgumentError(f"not an allowed revision: {revision!r}")
    return revision


def _bracket_end(pattern: str, start: int) -> int:
    """The index of the ``]`` closing the bracket expression at ``start`` (or the end)."""
    index = start + 1
    if pattern[index : index + 1] == "^":
        index += 1
    if pattern[index : index + 1] == "]":
        index += 1  # a leading ] is a literal member
    while index < len(pattern) and pattern[index] != "]":
        if pattern[index] == "[" and pattern[index + 1 : index + 2] in (":", "=", "."):
            close = pattern.find(pattern[index + 1] + "]", index + 2)
            index = len(pattern) if close == -1 else close + 2
            continue
        index += 1
    return index


_REGEX_SUBSET = (
    "Allowed: literal text; \\w \\s \\d \\b and \\W \\S \\D; escaped punctuation (\\( \\. \\{); "
    ". [classes] ^ $; * + ? {m} {m,n} (n <= 50) right after one character, class, escape "
    "or .; at most 4 open-ended repeats (* +) and finite {m,n} bounds summing to 100 or "
    "less; (groups) that are never repeated; and up to 20 | alternatives."
)
# m and n of an interval; anything else after "{" isn't one.
_REPEAT = re.compile(r"\{([0-9]{1,3})(?:,([0-9]{1,3}))?\}")


def regex_problem(pattern: str, dialect: RegexDialect) -> str | None:
    """Why an extended regex (``git grep -E``, ``log --author=``) is outside the allowed
    subset, or ``None`` if it is inside it.

    An allowlist, not a denylist: the regex engine's memory grows with how a pattern
    repeats and alternates (``(a|b|...|()){50}`` costs gigabytes), so only shapes whose cost
    is linear in the pattern are accepted. Repeats apply to a single atom; groups are never
    repeated and never empty; there are at most ``REGEX_MAX_ALTERNATIVES`` alternatives
    and at most ``GREP_MAX_PATTERN_CHARS`` characters. CPU is budgeted too: at most
    ``REGEX_MAX_UNBOUNDED`` open-ended repeats, and finite upper bounds summing to at most
    ``REGEX_MAX_BOUND_TOTAL``.

    Examples:
        >>> regex_problem("(foo|bar)baz", BSD_REGEX) is None
        True
        >>> regex_problem("(ab)+", BSD_REGEX).startswith("groups can't be repeated")
        True
    """
    if len(pattern) > GREP_MAX_PATTERN_CHARS:
        return f"the pattern is limited to {GREP_MAX_PATTERN_CHARS} characters"
    depth = 0
    alternatives = 1
    unbounded = 0
    bound_total = 0
    previous = "start"  # "start" (of a branch), "atom", "anchor", "group" or "repeat"
    index = 0
    while index < len(pattern):
        char = pattern[index]
        step = 1
        if char == "\\":
            escaped = pattern[index + 1 : index + 2]
            if escaped == "b" or (escaped == "B" and dialect.not_word_boundary):
                previous = "anchor"
            elif escaped in tuple("wsdWSD") or (escaped and _is_punctuation(escaped)):
                previous = "atom"
            else:
                return f"\\{escaped} is not supported. {_REGEX_SUBSET}"
            step = 2
        elif char == "[":
            end = _bracket_end(pattern, index)
            if end == len(pattern):
                return f"a [class] is not closed. {_REGEX_SUBSET}"
            previous, step = "atom", end - index + 1
        elif char in "^$":
            previous = "anchor"
        elif char == "(":
            depth, previous = depth + 1, "start"
        elif char == ")":
            if depth == 0:
                return f"a ) has no matching (; escape it as \\). {_REGEX_SUBSET}"
            if previous == "start":
                return f"empty groups and empty alternatives are refused. {_REGEX_SUBSET}"
            depth, previous = depth - 1, "group"
        elif char == "|":
            alternatives += 1
            if previous == "start":
                return f"empty groups and empty alternatives are refused. {_REGEX_SUBSET}"
            if alternatives > REGEX_MAX_ALTERNATIVES:
                return f"at most {REGEX_MAX_ALTERNATIVES} alternatives. {_REGEX_SUBSET}"
            previous = "start"
        elif char in "*+?{":
            repeat = _REPEAT.match(pattern, index) if char == "{" else None
            if char == "{" and repeat is None:
                return f"{{ starts a repeat {{m}} or {{m,n}}; escape it as \\{{. {_REGEX_SUBSET}"
            if repeat is not None:
                low, high = int(repeat[1]), int(repeat[2] or repeat[1])
                if not low <= high <= REGEX_MAX_REPEAT:
                    return f"repeats are limited to {{m,n}} with m <= n <= {REGEX_MAX_REPEAT}"
                step = len(repeat[0])
                bound_total += high
            elif char in "*+":
                unbounded += 1
            if unbounded > REGEX_MAX_UNBOUNDED:
                return f"at most {REGEX_MAX_UNBOUNDED} open-ended repeats (* +). {_REGEX_SUBSET}"
            if bound_total > REGEX_MAX_BOUND_TOTAL:
                return (
                    f"{{m,n}} upper bounds may add up to {REGEX_MAX_BOUND_TOTAL}. {_REGEX_SUBSET}"
                )
            if previous == "group":
                return f"groups can't be repeated. {_REGEX_SUBSET}"
            if previous != "atom":
                return f"a repeat must follow one character, class, escape or '.'. {_REGEX_SUBSET}"
            previous = "repeat"
        else:
            previous = "atom"  # a literal, ".", or a lone "]" or "}" (literals in ERE)
        index += step
    if depth:
        return f"a ( is not closed. {_REGEX_SUBSET}"
    if previous == "start" and alternatives > 1:
        return f"empty groups and empty alternatives are refused. {_REGEX_SUBSET}"
    return None


def _is_punctuation(char: str) -> bool:
    return char.isascii() and char.isprintable() and not char.isalnum()


_SHORTHAND_CLASSES = {
    "w": "[[:alnum:]_]",
    "W": "[^[:alnum:]_]",
    "s": "[[:space:]]",
    "S": "[^[:space:]]",
    "d": "[[:digit:]]",
    "D": "[^[:digit:]]",
}


def posix_ere(pattern: str, dialect: RegexDialect) -> str:
    """``pattern`` with ``\\w``, ``\\s``, ``\\d`` (and their negations) spelled as POSIX
    classes: agents write them, and the macOS regex library git uses doesn't know them.
    Where git's ``dialect`` has no ``\\b`` either, it becomes ``[[:<:]]`` or ``[[:>:]]``;
    otherwise ``\\b`` (and ``\\B``) pass through unchanged.

    Example:
        >>> posix_ere("def \\w+", BSD_REGEX)
        'def [[:alnum:]_]+'
    """
    native = dialect.native_word_boundaries
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "[":
            end = _bracket_end(pattern, index) + 1
            out.append(pattern[index:end])
            index = end
        elif char == "\\" and pattern[index + 1 : index + 2] == "b" and not native:
            # macOS regex has no \b; its word-start and word-end anchors need picking. After
            # a word character (or a repeat of one) it ends a word, otherwise it starts one.
            ends_word = index > 0 and (
                pattern[index - 1].isalnum() or pattern[index - 1] in "_+*?}]"
            )
            out.append("[[:>:]]" if ends_word else "[[:<:]]")
            index += 2
        elif char == "\\":
            escaped = pattern[index : index + 2]
            out.append(_SHORTHAND_CLASSES.get(escaped[1:], escaped))
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def is_plain_text(pattern: str) -> bool:
    """Whether ``pattern`` has no regex metacharacters, so it can be searched literally."""
    return not _REGEX_METACHARACTERS.intersection(pattern)


def check_pattern(pattern: str, dialect: RegexDialect) -> str:
    """Return a ``repo_grep`` pattern if it is one line and passes ``regex_problem``.

    Raises:
        InvalidSearchError: empty, with control characters (including newlines), or refused
            by ``regex_problem``.
    """
    if not pattern or _has_control_characters(pattern):
        raise InvalidSearchError("the pattern must be one non-empty line")
    problem = regex_problem(pattern, dialect)
    if problem is not None:
        raise InvalidSearchError(problem)
    return pattern


def check_glob(glob: str) -> str:
    """The git pathspec for a ``repo_grep`` glob; a glob without ``/`` matches at any depth.

    Raises:
        InvalidSearchError: absolute, ``..``, ``.git``, pathspec magic (a leading ``:``),
            control characters, or too long.
    """
    if not glob or len(glob) > _MAX_GLOB_CHARS or _has_control_characters(glob):
        raise InvalidSearchError(f"not a usable glob: {glob!r}")
    parts = glob.split("/")
    if glob.startswith(("/", ":")) or ".." in parts or any(p.lower() == ".git" for p in parts):
        raise InvalidSearchError(f"globs are relative to the PR root: {glob!r}")
    return f":(glob){glob}" if "/" in glob else f":(glob)**/{glob}"


def _refuse(cmd: str, token: str) -> UnsafeGitArgumentError:
    return UnsafeGitArgumentError(
        f"git {cmd} does not allow {token!r}; allowed options: {_ALLOWED_HELP[cmd]}"
    )


def _option(
    cmd: str, token: str, following: str | None, dialect: RegexDialect
) -> tuple[tuple[str, ...], int]:
    """Check one option; return its normalized argv and how many tokens it used."""
    if token in _FLAGS[cmd]:
        return (token,), 1
    name, equals, value = token.partition("=")
    if equals and name in _VALUED_OPTIONS[cmd]:
        if not value or len(value) > _MAX_OPTION_VALUE_CHARS:
            raise UnsafeGitArgumentError(f"{name} needs a value of 1-100 characters")
        problem = regex_problem(value, dialect) if name == "--author" else None
        if problem is not None:
            raise UnsafeGitArgumentError(f"--author: {problem}")
        return (f"{name}={posix_ere(value, dialect)}" if name == "--author" else token,), 1
    joined = token[2:]
    if cmd == "log" and token.startswith("-n"):
        count, used = (joined, 1) if joined else (following, 2)
        if count is None or not _COUNT.fullmatch(count) or int(count) > LOG_MAX_COUNT:
            raise UnsafeGitArgumentError(f"-n takes a count from 1 to {LOG_MAX_COUNT}")
        return ("-n", count), used
    if cmd == "blame" and token.startswith("-L"):
        span, used = (joined, 1) if joined else (following, 2)
        match = _LINE_SPAN.fullmatch(span or "")
        if match is None or not 1 <= int(match[1]) <= int(match[2]):
            raise UnsafeGitArgumentError("-L takes <start>,<end> line numbers, start <= end")
        return ("-L", f"{int(match[1])},{int(match[2])}"), used
    raise _refuse(cmd, token)


def plan_git_command(cmd: str, args: Sequence[str], *, dialect: RegexDialect) -> GitPlan:
    """Check a ``repo_git`` request against the allowlist.

    Revisions come before ``--`` and worktree paths after it. ``show`` also takes one
    ``<rev>:<path>``; ``blame`` takes ``[<rev>] <path>`` (with or without ``--``).

    Raises:
        UnsafeGitArgumentError: anything outside the allowlist.
        UnsafePathError: a path that is not a clean relative path.

    Example:
        >>> plan_git_command("log", ["-n", "5", "--", "src/app.py"], dialect=BSD_REGEX).paths
        ('src/app.py',)
    """
    if cmd not in _FLAGS:
        raise UnsafeGitArgumentError(f"repo_git runs log, show or blame, not {cmd!r}")
    if len(args) > _MAX_ARGS:
        raise UnsafeGitArgumentError(f"at most {_MAX_ARGS} arguments")
    for token in args:
        if len(token) > _MAX_ARG_CHARS or _has_control_characters(token):
            raise UnsafeGitArgumentError(f"not an allowed argument: {token!r}")
    tokens, paths = list(args), []
    has_separator = "--" in tokens
    if has_separator:
        split = tokens.index("--")
        tokens, paths = tokens[:split], tokens[split + 1 :]
    options: list[str] = []
    positionals: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            positionals.append(token)
            index += 1
            continue
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        normalized, used = _option(cmd, token, following, dialect)
        options.extend(normalized)
        index += used
    if cmd == "blame" and not has_separator and positionals:
        paths = [positionals.pop()]
    blobs: list[tuple[str, str]] = []
    revisions: list[str] = []
    for positional in positionals:
        revision, colon, blob_path = positional.partition(":")
        if colon and cmd == "show":
            blobs.append((check_revision(revision), normalize_relative_path(blob_path)))
        else:
            revisions.append(check_revision(positional))
    if blobs and (len(positionals) > 1 or paths):
        raise UnsafeGitArgumentError("show <rev>:<path> takes no other revisions or paths")
    if len(revisions) > (1 if cmd == "blame" else _MAX_REVISIONS) or len(paths) > _MAX_PATHS:
        raise UnsafeGitArgumentError(f"too many revisions or paths for git {cmd}")
    if cmd == "blame" and len(paths) != 1:
        raise UnsafeGitArgumentError("blame takes exactly one file: [<rev>] <path>")
    return GitPlan(
        cmd,
        tuple(options),
        tuple(revisions),
        tuple(blobs),
        tuple(normalize_relative_path(path) for path in paths),
    )
