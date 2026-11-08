"""Read-only diffs of the files a box wrote, for the pane's inspector.

Git never reads the working tree here, because reading it runs the repository's clean filters
(`filter.<name>.clean` in `.git/config`), which the agent can write. Git only reads the committed
blob (`git cat-file`, which applies no filters) and the plugin reads the current file itself, with
a size limit, and builds the diff with difflib. Paths go to git as `HEAD:<path>` object names, not
as pathspecs, so pathspec magic like `:(top)` has no effect. Paths must stay inside the root.
"""

from __future__ import annotations

import difflib
import logging
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_FILES = 8
MAX_FILE_BYTES = 512_000
MAX_DIFF_CHARS = 20_000
TIMEOUT_S = 5
GIT_SAFE = ("-c", "core.fsmonitor=false", "-c", "core.quotepath=off")
# Git gets only these from the gateway's environment. Other variables (`GIT_DIR`, `GIT_CONFIG_*`,
# provider keys) could point git elsewhere or reach a program the repository runs. `HOME`,
# `XDG_CONFIG_HOME` and `USERPROFILE` locate the user's global git config (`safe.directory`), and
# Windows needs `SYSTEMROOT` to start git.
GIT_ENV_KEEP = ("PATH", "HOME", "LANG", "XDG_CONFIG_HOME", "SYSTEMROOT", "USERPROFILE")
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_LITERAL_PATHSPECS": "1",
    # A promisor remote in .git/config would fetch missing objects with a command the repository
    # chooses (`ext::` URLs).
    "GIT_NO_LAZY_FETCH": "1",
}


@dataclass(frozen=True)
class FileDiff:
    path: str
    status: str  # modified, added, unchanged, missing, outside, binary, too_large or error
    diff: str = ""
    truncated: bool = False


def box_diffs(root: str, paths: list[str]) -> list[dict[str, Any]]:
    """Diffs for up to MAX_FILES of `paths` (relative to `root`), newest last."""
    chosen = paths[-MAX_FILES:]
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        return [asdict(FileDiff(p, "error", "The canvas root does not exist.")) for p in chosen]
    prefix = _git(base, "rev-parse", "--show-prefix")
    if prefix is None:
        return [asdict(FileDiff(p, "error", "The canvas root is not a git repository.")) for p in chosen]
    has_head = _git(base, "rev-parse", "--verify", "--quiet", "HEAD^{commit}") is not None
    return [asdict(file_diff(base, prefix.strip(), has_head, p)) for p in chosen]


def file_diff(base: Path, prefix: str, has_head: bool, rel: str) -> FileDiff:
    target = (base / rel).resolve()
    if not target.is_relative_to(base) or target == base:
        return FileDiff(rel, "outside")
    inside = target.relative_to(base).as_posix()
    old = _committed(base, f"HEAD:{prefix}{inside}") if has_head else None
    if isinstance(old, FileDiff):
        return FileDiff(rel, old.status, old.diff)
    new = _current(target)
    if isinstance(new, FileDiff):
        if new.status == "missing" and old is not None:
            return _unified(rel, "missing", old, "")
        return FileDiff(rel, new.status, new.diff)
    if old is None:
        return _unified(rel, "added", "", new)
    if old == new:
        return FileDiff(rel, "unchanged")
    return _unified(rel, "modified", old, new)


def _committed(base: Path, name: str) -> str | FileDiff | None:
    """The committed text of `name`, None when HEAD has no such file."""
    if _git(base, "cat-file", "-e", name) is None:
        return None
    size = _git(base, "cat-file", "-s", name)
    if size is None or not size.strip().isdigit():
        return FileDiff("", "error", "git could not read the committed file.")
    if int(size) > MAX_FILE_BYTES:
        return FileDiff("", "too_large")
    data = _git(base, "cat-file", "blob", name, text=False)
    if not isinstance(data, bytes):
        return FileDiff("", "error", "git could not read the committed file.")
    return _decode(data)


def _current(target: Path) -> str | FileDiff:
    try:
        if not target.is_file():
            return FileDiff("", "missing")
        if target.stat().st_size > MAX_FILE_BYTES:
            return FileDiff("", "too_large")
        with target.open("rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        logger.warning("hermes-canvas: could not read %s: %s", target, exc)
        return FileDiff("", "error", "The file could not be read.")
    if len(data) > MAX_FILE_BYTES:
        return FileDiff("", "too_large")
    return _decode(data)


def _decode(data: bytes) -> str | FileDiff:
    if b"\0" in data[:8192]:
        return FileDiff("", "binary")
    return data.decode("utf-8", errors="replace")


def _unified(rel: str, status: str, old: str, new: str) -> FileDiff:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{rel}" if old else "/dev/null",
        tofile=f"b/{rel}" if new else "/dev/null",
        n=3,
    )
    out: list[str] = []
    size = 0
    for line in lines:
        if not line.endswith("\n"):
            line += "\n"
        if size + len(line) > MAX_DIFF_CHARS:
            return FileDiff(rel, status, "".join(out), truncated=True)
        out.append(line)
        size += len(line)
    return FileDiff(rel, status, "".join(out))


def _git(base: Path, *args: str, text: bool = True) -> str | bytes | None:
    try:
        proc = subprocess.run(
            ["git", *GIT_SAFE, *args],
            cwd=base,
            capture_output=True,
            timeout=TIMEOUT_S,
            check=False,
            env={**{k: os.environ[k] for k in GIT_ENV_KEEP if k in os.environ}, **GIT_ENV},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("hermes-canvas: git %s failed: %s", args[0], exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace") if text else proc.stdout
