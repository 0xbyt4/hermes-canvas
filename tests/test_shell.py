from __future__ import annotations

import time

import pytest

from canvas.binding import Binding, check_globs
from canvas.model import Box, Doc
from canvas.shell import code_paths, shell_scan, shell_write_paths


@pytest.mark.parametrize(
    "command, paths",
    [
        # A comment does not hide the writes after it, and `#` inside a word is not a comment.
        ("# write the page\ncat > src/refund/page.tsx <<EOF\nx\nEOF", ["src/refund/page.tsx"]),
        ("mkdir -p src/refund # dir\necho x > src/refund/a.ts", ["src/refund/a.ts"]),
        ("echo a#b > out.txt", ["out.txt"]),
        ('echo "#x" > out.txt', ["out.txt"]),
        # The file descriptor before a redirect is not a file.
        ("cp src/a.py src/b.py 2>/dev/null", ["src/b.py"]),
        ("touch a.txt 2>/dev/null", ["a.txt"]),
        ("npx vitest run 2>&1 | tail -5", []),
        # `<<<`, arithmetic and `<<` inside quotes are not heredocs.
        ("grep x <<< hello\necho x > src/removed.py", ["src/removed.py"]),
        ("echo $((1<<FOO))\necho x > src/removed.py", ["src/removed.py"]),
        ("python -c 'print(1<<x)'\necho x > src/removed.py", ["src/removed.py"]),
        # Heredoc bodies are never read as commands, with any delimiter quoting.
        ("cat <<\\EOF\n<p>a > b</p>\nEOF", []),
        ("cat > f.html <<-'END'\n\t<p>a > b</p>\n\tEND\necho done > g.txt", ["f.html", "g.txt"]),
        # Line continuations.
        ("cp a.ts \\\n  src/b.ts", ["src/b.ts"]),
        # Joined punctuation.
        ("(cd /r; cat x)> out.txt", ["out.txt"]),
    ],
)
def test_review_cases(command: str, paths: list[str]) -> None:
    assert shell_write_paths(command) == paths


@pytest.mark.parametrize(
    "command, paths",
    [
        ("cd web && cat > src/refund/page.tsx <<EOF\nx\nEOF", ["web/src/refund/page.tsx"]),
        ("cd src && echo x > refund/a.ts", ["src/refund/a.ts"]),
        ("cd /repo && echo x > src/a.ts", ["/repo/src/a.ts"]),
        ("cd $HOME && echo x > a.ts; echo y > /abs/b.ts", ["/abs/b.ts"]),
        ("cd ~/x && echo x > a.ts", []),
        ("echo x > $OUT", []),
    ],
)
def test_cd_is_followed(command: str, paths: list[str]) -> None:
    assert shell_write_paths(command) == paths


@pytest.mark.parametrize(
    "command, paths",
    [
        # A `<<` inside a comment does not start a heredoc.
        ("# write with cat <<EOF\necho x > src/refund/a.ts\necho y > z.ts", ["src/refund/a.ts", "z.ts"]),
        # Quotes inside a comment do not hide the commands after it.
        ("# 'quoted\necho x > src/refund/a.ts", ["src/refund/a.ts"]),
        ('echo hi # "unbalanced\necho x > src/refund/a.ts', ["src/refund/a.ts"]),
        ("# don't\necho x > a.ts", ["a.ts"]),
        ("echo $# > n.txt", ["n.txt"]),
        # popd goes back to the folder before pushd.
        ("pushd web\npopd\necho x > src/refund/a.ts", ["src/refund/a.ts"]),
        ("popd\necho x > a.ts; echo y > /abs/b.ts", ["/abs/b.ts"]),
    ],
)
def test_comments_and_popd(command: str, paths: list[str]) -> None:
    assert shell_write_paths(command) == paths


def test_a_long_command_keeps_the_paths_before_the_scan_limit() -> None:
    command = 'echo x > src/refund/a.ts; python3 -c "' + "a" * 80_000 + '"'
    assert shell_write_paths(command) == ["src/refund/a.ts"]
    lines = "echo x > src/refund/a.ts\n" + ("echo y\n" * 20_000)
    assert shell_write_paths(lines) == ["src/refund/a.ts"]


def test_the_start_folder_is_used_for_relative_paths() -> None:
    assert shell_write_paths("echo x > src/a.ts", "/repo/web") == ["/repo/web/src/a.ts"]
    assert shell_write_paths("echo x > src/a.ts; echo y > /abs/b.ts", None) == ["/abs/b.ts"]
    assert shell_write_paths("cd /repo && echo x > src/a.ts", None) == ["/repo/src/a.ts"]


def test_scans_stay_fast_on_huge_input() -> None:
    big = 'python3 -c "' + "a" * 1_000_000 + '" > out.txt'
    code = '"' + "a.a." * 400_000
    started = time.perf_counter()
    shell_write_paths(big)
    code_paths(code)
    code_paths("'" + "QUJD" * 400_000 + "='")
    assert time.perf_counter() - started < 0.5


def test_code_paths_reads_path_literals() -> None:
    code = "open('src/a.ts', 'w'); Path(\"/repo/b/c.py\").write_text('hi'); x = 'plain'; y = 'a b/c'"
    assert code_paths(code) == ["src/a.ts", "/repo/b/c.py"]


@pytest.mark.parametrize(
    "check, globs",
    [
        ("uv run pytest -c tests/pytest.ini tests/unit/test_cart.py", ["tests/unit/test_cart.py"]),
        ("npm --prefix apps/web test", []),
        ("cargo test --manifest-path crates/a/Cargo.toml", []),
    ],
)
def test_check_globs_skip_flag_values(check: str, globs: list[str]) -> None:
    assert check_globs(check) == globs


def test_a_test_folder_shared_by_two_checks_belongs_to_neither() -> None:
    boxes = [
        Box(id="cart", label="Cart", files=["src/cart/**"], check="uv run pytest tests/unit -k cart"),
        Box(id="tax", label="Tax", files=["src/tax/**"], check="uv run pytest tests/unit -k tax"),
        Box(id="pay", label="Pay", files=["src/pay/**"], check="npx vitest run tests/pay"),
    ]
    binding = Binding.build(Doc(id="cv-0123456789", session_id="s", title="t", root="/r", boxes=boxes))
    assert binding.boxes_for_path("tests/unit/test_cart.py") == []
    assert binding.boxes_for_path("tests/pay/a.test.ts") == ["pay"]


def test_check_paths_do_not_widen_the_veto() -> None:
    box = Box(id="web", label="Web", files=["apps/web/src/**"], check="npx vitest run apps/web", removed=True)
    binding = Binding.build(Doc(id="cv-0123456789", session_id="s", title="t", root="/r", boxes=[box]))
    assert binding.removed_box_for_path("apps/web/src/a.ts") == "web"
    assert binding.removed_box_for_path("apps/web/README.md") is None


@pytest.mark.parametrize(
    "command",
    [
        'python3 -c "\nx = 1 << y\n" && echo x > src/refund/a.ts',
        'git commit -m "docs\nuse cat <<EOF to write\n" && echo x > src/refund/a.ts',
    ],
)
def test_a_quoted_shift_over_several_lines_is_not_a_heredoc(command: str) -> None:
    assert shell_write_paths(command) == ["src/refund/a.ts"]


def test_the_scan_reports_where_the_command_ends() -> None:
    assert shell_scan("cd /repo && echo x > a.ts", None) == (["/repo/a.ts"], "/repo")
    assert shell_scan("(cd /x) && echo y > b.ts", "/repo") == (["/repo/b.ts"], "/repo")
    assert shell_scan("cd $HOME", "/repo")[1] is None
    assert shell_scan('echo "' + "a" * 70_000 + '"', "/repo")[1] is None
