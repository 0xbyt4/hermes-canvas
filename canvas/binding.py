"""Match tool calls to canvas boxes: written paths to file globs, terminal commands to checks.

Everything here is pure and in memory, because `pre_tool_call` uses it and fails closed when slow.
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field

from .model import Box, CanvasError, Doc

WRITE_TOOLS = ("write_file", "patch")
GOAL_TAG_RE = re.compile(r"\[canvas:(cv-[0-9a-f]{10})/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})\]")
_V4A_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)
_STDERR_REDIRECT_RE = re.compile(r"^2>\s*(?:&1|\S+)\s*")
_CD_PREFIX_RE = re.compile(r"^\s*cd\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s*(?:&&|;)\s*")
_STDERR_SUFFIX_RE = re.compile(r"\s+2>&1\s*$")
_WILDCARDS = set("*?")
_PATH_ARG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@+-]*(?:/[A-Za-z0-9_.@+-]+)+/?$")
_TEST_MODULE_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")


# Flags whose value is a path that is not a test target (config files, other package roots).
_PATH_VALUE_FLAGS = {
    "-c",
    "-C",
    "-p",
    "--config",
    "--prefix",
    "--manifest-path",
    "--rootdir",
    "--project",
    "--dir",
    "--cwd",
}


def check_globs(check: str) -> list[str]:
    """File globs implied by the path arguments of a check command.

    The tests a check runs belong to its box, even when the plan's `files` list only the source:
    `npx vitest run tests/payments` covers `tests/payments/**` and `tests/payments*` (vitest filters
    by prefix), `tests/receipt.test.ts` covers that file, and `python3 -m unittest tests.test_tax`
    covers `tests/test_tax.py`. Arguments without a `/` (like `tests`) are too broad and are skipped,
    and so are the values of flags like `-c` or `--prefix`.
    """
    try:
        tokens = shlex.split(normalize_command(check))
    except ValueError:
        return []
    globs: list[str] = []
    skip_next = False
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if token in _PATH_VALUE_FLAGS:
            skip_next = True
            continue
        if token.startswith("-") or ".." in token.split("/"):
            continue
        if _PATH_ARG_RE.match(token):
            path = token.rstrip("/")
            if posixpath.splitext(path)[1] and not token.endswith("/"):
                globs.append(path)
            else:
                globs += [f"{path}/**", f"{path}*"]
        elif _TEST_MODULE_RE.match(token) and "test" in token.lower():
            globs.append(token.replace(".", "/") + ".py")
    return globs


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a project-relative glob.

    - `**` spans directories; `*` and `?` stay inside one path segment.
    - `{a,b}` matches either alternative (no nesting).
    - A pattern that ends with `/`, or has no wildcard at all, also matches everything under it,
      so `src/payments`, `src/payments/` and `src/{api,ui}` cover the files under them.
    """
    pattern = pattern.strip()
    while pattern.startswith("./"):
        pattern = pattern[2:]
    if pattern.endswith("/"):
        pattern += "**"
    body = _translate(pattern)
    if not _WILDCARDS & set(pattern):
        body += "(?:/.*)?"
    return re.compile(body + r"\Z")


def _translate(pattern: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "{" and "}" in pattern[i:]:
            end = pattern.index("}", i)
            options = pattern[i + 1 : end].split(",")
            out.append("(?:" + "|".join(_translate(o) for o in options) + ")")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


def _real(path: str) -> str:
    """`realpath` that never raises, so /tmp vs /private/tmp and symlinks compare equal."""
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return os.path.normpath(path)


def relativize(path: str, root: str) -> str | None:
    """Return `path` relative to `root` in posix form, or None when it is outside the root.

    A relative `path` is taken as relative to `root`.
    """
    if not path or not root:
        return None
    expanded = os.path.expanduser(path)
    root_abs = os.path.expanduser(root)
    absolute = expanded if os.path.isabs(expanded) else os.path.join(root_abs, expanded)
    for candidate, base in (
        (os.path.normpath(absolute), os.path.normpath(root_abs)),
        (_real(absolute), _real(root_abs)),
    ):
        try:
            rel = os.path.relpath(candidate, base)
        except ValueError:  # different drives on Windows
            continue
        if rel != os.curdir and rel != os.pardir and not rel.startswith(os.pardir + os.sep):
            return posixpath.join(*rel.split(os.sep))
    return None


def normalize_globs(boxes: Iterable[Box], root: str) -> None:
    """Turn absolute globs under `root` into relative ones. Reject absolute globs outside it."""
    for box in boxes:
        fixed: list[str] = []
        for glob in box.files:
            expanded = os.path.expanduser(glob)
            if not os.path.isabs(expanded):
                fixed.append(glob)
                continue
            rel = relativize(expanded.rstrip("/"), root)
            if rel is None:
                raise CanvasError(f"box {box.id!r}: file glob {glob!r} is outside the project root {root}")
            fixed.append(rel + ("/" if expanded.endswith("/") else ""))
        box.files = fixed


def paths_from_args(tool_name: str, args: object) -> list[str]:
    """Paths a write tool call touches, as given by the model (may be relative)."""
    if tool_name not in WRITE_TOOLS or not isinstance(args, dict):
        return []
    if tool_name == "patch" and args.get("mode") == "patch":
        body = args.get("patch")
        if not isinstance(body, str):
            return []
        return [a or b for a, b in _V4A_FILE_RE.findall(body)]
    path = args.get("path")
    return [path] if isinstance(path, str) and path else []


def paths_from_result(result: object) -> list[str]:
    """Absolute paths Hermes reports it wrote (`resolved_path`, `files_modified`, ...).

    Hermes resolves relative paths against the session workspace, which can differ from the
    canvas root, so these are better than the model's arguments when present.
    """
    if not isinstance(result, dict):
        return []
    paths: list[str] = []
    for key in ("files_modified", "files_created", "files_deleted"):
        value = result.get(key)
        if isinstance(value, list):
            paths.extend(p for p in value if isinstance(p, str))
    resolved = result.get("resolved_path")
    if isinstance(resolved, str) and resolved not in paths:
        paths.append(resolved)
    return [p for p in paths if os.path.isabs(os.path.expanduser(p))]


def normalize_command(command: str) -> str:
    """Canonical form of a shell command for check matching.

    Drops a leading `cd <dir> &&` and a trailing `2>&1`, and collapses quoting and whitespace.
    """
    command = _CD_PREFIX_RE.sub("", command.strip())
    command = _STDERR_SUFFIX_RE.sub("", command)
    try:
        return " ".join(shlex.split(command))
    except ValueError:
        return " ".join(command.split())


def parse_goal_tag(goal: str) -> tuple[str, str] | None:
    """Find `[canvas:<doc>/<box>]` in a subagent goal."""
    match = GOAL_TAG_RE.search(goal or "")
    return (match.group(1), match.group(2)) if match else None


def goal_tag(doc_id: str, box_id: str) -> str:
    return f"[canvas:{doc_id}/{box_id}]"


def owned_check_globs(doc: Doc) -> dict[str, list[str]]:
    """The check paths each active component box owns.

    Test paths from a check count as the box's files, unless another box's check names the same
    path (a shared test folder). They only count writes: the veto uses `files` only.
    """
    implied = {b.id: check_globs(b.check) for b in doc.boxes if b.kind == "component" and not b.removed}
    seen: dict[str, int] = {}
    for globs in implied.values():
        for g in set(globs):
            seen[g] = seen.get(g, 0) + 1
    return {box_id: [g for g in globs if seen[g] == 1] for box_id, globs in implied.items()}


def check_test_paths(doc: Doc, box: Box) -> list[str]:
    """The check paths of `box` to name to an agent: folders as `dir/**`, files as they are."""
    owned = owned_check_globs(doc).get(box.id, [])
    return [g for g in owned if (g.endswith("/**") or not g.endswith("*")) and g not in box.files]


@dataclass
class Binding:
    """Compiled matchers for one document revision."""

    doc_id: str
    root: str
    revision: int
    active: list[tuple[str, list[re.Pattern[str]]]] = field(default_factory=list)
    components: set[str] = field(default_factory=set)
    removed: list[tuple[str, list[re.Pattern[str]]]] = field(default_factory=list)
    checks: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def build(cls, doc: Doc) -> Binding:
        binding = cls(doc_id=doc.id, root=doc.root, revision=doc.revision)
        implied = owned_check_globs(doc)
        for box in doc.boxes:
            extra = implied.get(box.id, [])
            if not box.files and not extra:
                continue
            if not box.removed and box.kind in ("component", "drift"):
                binding.active.append((box.id, [glob_to_regex(g) for g in [*box.files, *extra]]))
                if box.kind == "component":
                    binding.components.add(box.id)
            elif box.removed and box.kind == "component" and box.files:
                binding.removed.append((box.id, [glob_to_regex(g) for g in box.files]))
        for box in doc.active_boxes():
            if box.check:
                binding.checks.append((box.id, normalize_command(box.check)))
        return binding

    def boxes_for_path(self, rel: str) -> list[str]:
        return [box_id for box_id, pats in self.active if any(p.match(rel) for p in pats)]

    def removed_box_for_path(self, rel: str) -> str | None:
        """The removed component box that owns `rel`, unless an active component box also owns it.

        Drift boxes do not count: they only record where writes went, they do not claim a path.
        """
        if any(box_id in self.components for box_id in self.boxes_for_path(rel)):
            return None
        for box_id, pats in self.removed:
            if any(p.match(rel) for p in pats):
                return box_id
        return None

    def boxes_for_command(self, command: str) -> list[str]:
        """Boxes whose check is exactly this command (after normalization).

        Exact match only: `echo '<check>'`, `<check> || true` or a partial run with extra
        arguments must not count as running the check.
        """
        normalized = normalize_command(command)
        if not normalized:
            return []
        return [box_id for box_id, check in self.checks if check == normalized]

    def near_checks(self, command: str) -> list[tuple[str, str]]:
        """(box id, check) for checks this command starts with but does not equal.

        `<check>; echo "exit=$?"`, `<check> | tail` or `<check> || true` run the check, but the
        exit code the canvas sees is not the check's, so they are not counted. The agent is told
        to run the exact command instead.
        """
        # A newline separates commands like `;` does; normalizing would turn it into a space.
        normalized = normalize_command(command.replace("\n", " ; "))
        found = []
        for box_id, check in self.checks:
            if normalized == check or not normalized.startswith(check):
                continue
            # Only shell operators after the check: extra arguments make it another command.
            rest = _STDERR_REDIRECT_RE.sub("", normalized[len(check) :].lstrip())
            if not rest or rest[:1] in (";", "|", "&", ">"):  # empty: only a redirect was added
                found.append((box_id, check))
        return found
