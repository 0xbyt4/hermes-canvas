"""Which files a terminal command or an `execute_code` script may write.

Agents often write files with the shell (`cat > f <<EOF`, `tee`, `cp`, `sed -i`) or from a script
instead of the write tools, so the veto and the write count read these too. This is a best effort
that reads redirects and the common file-writing commands, not every way a shell can write.

`pre_tool_call` calls this, and Hermes treats a slow hook badly, so every scan is linear and the
scanned text has a size limit.
"""

from __future__ import annotations

import posixpath
import re
import shlex

MAX_SCAN = 64_000
MAX_CODE_SCAN = 512_000
_NL = "__hc_newline__"
_OPERATORS = (
    "&>>",
    "&>",
    ">>",
    ">|",
    ">&",
    "<<<",
    "<<",
    "&&",
    "||",
    "|&",
    ";;",
    ">",
    "<",
    "|",
    "&",
    ";",
    "(",
    ")",
)
_REDIRECTS = {">", ">>", ">|", "&>", "&>>", ">&"}
_INPUTS = {"<", "<<", "<<<"}
_SEPARATORS = {";", ";;", "&&", "||", "|", "|&", "&", "(", ")", _NL}
_PUNCT = set("();<>|&")
_WORD_BREAK = set(" \t\n;&|()<>")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC_AT_RE = re.compile(r"<<(-?)\s*(\\?)(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\3")
_CODE_LITERAL_RE = re.compile(r"""(['"])([^'"\s\\]{1,512})\1""")
_PATHISH_RE = re.compile(r"^[A-Za-z0-9_./@+-]+$")


def shell_write_paths(command: str, cwd: str | None = "") -> list[str]:
    """Paths a shell command writes. See `shell_scan`."""
    return shell_scan(command, cwd)[0]


def shell_scan(command: str, cwd: str | None = "") -> tuple[list[str], str | None]:
    """The paths a shell command writes, and the directory it ends in.

    `cwd` is the directory the command starts in: "" for the canvas root, an absolute path, or
    None when it is unknown (then only absolute paths and paths after an absolute `cd` count).
    Relative paths are resolved from it, after any `cd` in the command; a path after a `cd` that
    cannot be followed is left out. The end directory is None when it cannot be known.
    """
    if not isinstance(command, str) or not command:
        return [], cwd
    text = command.replace("\\\r\n", " ").replace("\\\n", " ")
    stripped = _strip_heredocs(text)
    cut = _cut(stripped)
    text = _strip_comments(cut)
    lexer = shlex.shlex(text.replace("\n", f" {_NL} "), posix=False, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    raw: list[str] = []
    complete = len(cut) == len(stripped)
    try:
        for token in lexer:
            raw.append(token)
    except ValueError:  # an unbalanced quote, or the scan limit cut a quote: keep what came before
        complete = False
    paths, end = _scan(_split_operators(raw), cwd)
    return paths, end if complete else None


def _cut(text: str) -> str:
    """The first MAX_SCAN characters, ending at a line end when there is one."""
    if len(text) <= MAX_SCAN:
        return text
    end = text.rfind("\n", 0, MAX_SCAN)
    return text[:end] if end > 0 else text[:MAX_SCAN]


def _split_operators(tokens: list[str]) -> list[str]:
    """shlex joins neighbouring punctuation (`)>`, `;>`): split it into shell operators."""
    out: list[str] = []
    for token in tokens:
        if not token or not set(token) <= _PUNCT:
            out.append(token)
            continue
        i = 0
        while i < len(token):
            op = next((o for o in _OPERATORS if token.startswith(o, i)), token[i])
            out.append(op)
            i += len(op)
    return out


def _scan(tokens: list[str], start: str | None) -> tuple[list[str], str | None]:
    paths: list[str] = []
    cwd: str | None = start  # "" = the canvas root; None = unknown after a `cd` we cannot follow
    words: list[str] = []
    expect = ""  # "target" after an output redirect, "input" after an input redirect
    saved: list[str | None] = []
    pushed: list[str | None] = []

    def add(token: str) -> None:
        path = _unquote(token)
        if path and not path.isdigit() and path != "-":
            resolved = _resolve(cwd, path)
            if resolved is not None:
                paths.append(resolved)

    def flush() -> None:
        nonlocal cwd
        plain = [_unquote(w) for w in words]
        if plain and plain[0] in ("cd", "pushd"):
            if plain[0] == "pushd":
                pushed.append(cwd)
            cwd = _change_dir(cwd, plain[1] if len(plain) > 1 else "")
        elif plain and plain[0] == "popd":
            cwd = pushed.pop() if pushed else None
        else:
            for target in _command_targets(plain):
                resolved = _resolve(cwd, target)
                if resolved is not None:
                    paths.append(resolved)
        words.clear()

    for token in tokens:
        if expect:
            kind, expect = expect, ""
            if kind == "target" and token not in _SEPARATORS:
                add(token)
                continue
            if kind == "input" and token not in _SEPARATORS:
                continue
        if token in _SEPARATORS:
            flush()
            if token == "(":
                saved.append(cwd)  # a subshell's `cd` does not reach the commands after it
            elif token == ")" and saved:
                cwd = saved.pop()
        elif token in _REDIRECTS:
            if words and words[-1].isdigit():
                words.pop()  # the file descriptor of `2>err.log`
            expect = "target"
        elif token in _INPUTS:
            expect = "input"
        else:
            words.append(token)
    flush()
    end = None if saved else cwd  # an unclosed subshell: the end directory is unclear
    return [p for p in dict.fromkeys(paths) if p != "/dev/null"], end


def _unquote(token: str) -> str:
    if not any(c in token for c in "'\"\\"):
        return token
    try:
        parts = shlex.split(token)
    except ValueError:
        return ""
    return "".join(parts)


def _resolve(cwd: str | None, path: str) -> str | None:
    if "$" in path or "`" in path or path.startswith("~"):
        return None
    if path.startswith("/"):
        return posixpath.normpath(path)
    if cwd is None:
        return None
    return posixpath.normpath(posixpath.join(cwd, path)) if cwd else path


def _change_dir(cwd: str | None, target: str) -> str | None:
    if not target or target == "-" or target.startswith("~") or "$" in target or "`" in target:
        return None
    if target.startswith("/"):
        return posixpath.normpath(target)
    if cwd is None:
        return None
    return posixpath.normpath(posixpath.join(cwd, target)) if cwd else posixpath.normpath(target)


def _command_targets(words: list[str]) -> list[str]:
    while words and (_ENV_ASSIGN_RE.match(words[0]) or words[0] in ("sudo", "command", "env")):
        words = words[1:]
    if not words:
        return []
    name = posixpath.basename(words[0])
    args = words[1:]
    plain = [a for a in args if a and not a.startswith("-")]
    if name in ("tee", "touch"):
        return plain
    if name in ("cp", "mv", "install", "ln") and len(plain) >= 2:
        return [plain[-1]]
    if name == "sed":
        return _sed_files(args)
    return []


def _sed_files(args: list[str]) -> list[str]:
    in_place = False
    script_given = False
    files: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-e", "-f", "--expression", "--file"):
            script_given = True
            i += 2
            continue
        if a == "-i" and i + 1 < len(args) and (args[i + 1] == "" or args[i + 1].startswith(".")):
            in_place = True  # BSD sed: `-i ''` or `-i .bak`
            i += 2
            continue
        if a.startswith("-i") or a.startswith("--in-place"):
            in_place = True
        elif not a.startswith("-"):
            files.append(a)
        i += 1
    if not in_place:
        return []
    return files if script_given else files[1:]


def _strip_heredocs(command: str) -> str:
    """Drop heredoc bodies, so file contents (HTML, shell examples) are not read as commands."""
    lines = command.split("\n")
    out: list[str] = []
    quote = ""  # a quoted string can span lines: `python3 -c "x = 1 << y"` over three lines
    arith = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        starts, quote, arith = _heredoc_starts(line, quote, arith)
        for strip_tabs, delimiter in starts:
            while i < len(lines):
                body = lines[i].lstrip("\t") if strip_tabs else lines[i]
                i += 1
                if body.rstrip() == delimiter:
                    break
    return "\n".join(out)


def _heredoc_starts(line: str, quote: str, arith: int) -> tuple[list[tuple[bool, str]], str, int]:
    """(strip tabs, delimiter) of each `<<WORD` on the line that is outside quotes and arithmetic,
    and the quote and arithmetic state at the end of the line."""
    found: list[tuple[bool, str]] = []
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in ("'", '"'):
            quote = c
        elif c == "\\":
            i += 2
            continue
        elif c == "#" and _word_start(line, i):
            break  # the rest of the line is a comment
        elif line.startswith("((", i):
            arith += 1
            i += 2
            continue
        elif line.startswith("))", i) and arith:
            arith -= 1
            i += 2
            continue
        elif c == "<" and not arith and line.startswith("<<", i) and not line.startswith("<<<", i):
            if i == 0 or line[i - 1] != "<":
                m = _HEREDOC_AT_RE.match(line, i)
                if m:
                    found.append((m.group(1) == "-", m.group(4)))
                    i = m.end()
                    continue
        i += 1
    return found, quote, arith


def _word_start(text: str, i: int) -> bool:
    return i == 0 or text[i - 1] in _WORD_BREAK


def _strip_comments(text: str) -> str:
    """Drop shell comments: a `#` that starts a word outside quotes, up to the end of its line.

    Quotes inside a comment would otherwise confuse the lexer (`# don't` has an open quote).
    """
    out: list[str] = []
    quote = ""
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                out.append(text[i : i + 2])
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in ("'", '"'):
            quote = c
        elif c == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        elif c == "#" and _word_start(text, i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        out.append(c)
        i += 1
    return "".join(out)


def code_paths(code: str) -> list[str]:
    """String literals in a script that look like file paths (`'src/a.ts'`, `"/repo/x.py"`).

    `execute_code` scripts write files without passing through the write tools. The veto cannot
    tell reads from writes there, so it treats every path the script names as a possible write.
    """
    if not isinstance(code, str):
        return []
    found = [m.group(2) for m in _CODE_LITERAL_RE.finditer(code[:MAX_CODE_SCAN])]
    keep = [p for p in found if _PATHISH_RE.match(p) and ("/" in p or posixpath.splitext(p)[1])]
    return list(dict.fromkeys(keep))
