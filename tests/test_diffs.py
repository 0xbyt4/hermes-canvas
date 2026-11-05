from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from canvas.api import CanvasApi
from canvas.diffs import MAX_DIFF_CHARS, MAX_FILE_BYTES, box_diffs
from canvas.model import Box, CanvasError
from canvas.runtime import CanvasRuntime
from canvas.store import CanvasStore


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/cart.ts").write_text("export const a = 1\n")
    commit_all(tmp_path, "init")
    return tmp_path


def by_path(root: Path, paths: list[str]) -> dict[str, dict]:
    return {d["path"]: d for d in box_diffs(str(root), paths)}


def test_modified_added_unchanged_and_missing_files(repo: Path) -> None:
    (repo / "src/cart.ts").write_text("export const a = 2\n")
    (repo / "src/new.ts").write_text("export const b = 1\n")
    out = by_path(repo, ["src/cart.ts", "src/new.ts", "src/gone.ts"])
    assert out["src/cart.ts"]["status"] == "modified"
    assert "-export const a = 1" in out["src/cart.ts"]["diff"]
    assert "+export const a = 2" in out["src/cart.ts"]["diff"]
    assert out["src/new.ts"]["status"] == "added"
    assert "+export const b = 1" in out["src/new.ts"]["diff"]
    assert out["src/gone.ts"]["status"] == "missing"
    git(repo, "checkout", "--", "src/cart.ts")
    assert by_path(repo, ["src/cart.ts"])["src/cart.ts"]["status"] == "unchanged"


def test_a_deleted_tracked_file_shows_its_removed_lines(repo: Path) -> None:
    (repo / "src/cart.ts").unlink()
    [d] = box_diffs(str(repo), ["src/cart.ts"])
    assert d["status"] == "missing" and "-export const a = 1" in d["diff"]


def test_paths_outside_the_root_are_refused(repo: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    secret = tmp_path_factory.mktemp("other") / "secret.txt"
    secret.write_text("do not show\n")
    (repo / "link.txt").symlink_to(secret)
    out = by_path(repo, ["../other/secret.txt", "link.txt"])
    assert {d["status"] for d in out.values()} == {"outside"}
    assert all(d["diff"] == "" for d in out.values())


def test_a_folder_without_git_gives_an_error(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    [d] = box_diffs(str(tmp_path), ["a.py"])
    assert (d["status"], d["diff"]) == ("error", "The canvas root is not a git repository.")


def test_repository_commands_never_run(repo: Path) -> None:
    marker = repo.parent / "command-ran"
    for key in ("diff.evil.command", "diff.evil.textconv", "filter.evil.clean", "filter.evil.smudge"):
        git(repo, "config", key, f"touch {marker}")
    git(repo, "config", "filter.evil.process", f"touch {marker}")
    (repo / ".gitattributes").write_text("* diff=evil filter=evil\n")
    (repo / "src/cart.ts").write_text("export const a = 3\n")
    (repo / "src/new.ts").write_text("x\n")
    out = by_path(repo, ["src/cart.ts", "src/new.ts"])
    assert out["src/cart.ts"]["status"] == "modified"
    assert out["src/new.ts"]["status"] == "added"
    assert not marker.exists()


def test_pathspec_magic_cannot_reach_outside_a_subfolder_root(repo: Path) -> None:
    (repo / "top-secret.txt").write_text("secret\n")
    (repo / "app").mkdir()
    (repo / "app/a.txt").write_text("a\n")
    commit_all(repo, "more")
    out = by_path(repo / "app", [":(top)top-secret.txt", ":/top-secret.txt", "*", "a.txt"])
    assert all("secret" not in d["diff"] for d in out.values())
    assert out["a.txt"]["status"] == "unchanged"
    assert {out[p]["status"] for p in (":(top)top-secret.txt", ":/top-secret.txt", "*")} == {"missing"}


def test_a_subfolder_root_diffs_against_the_right_committed_file(repo: Path) -> None:
    (repo / "app").mkdir()
    (repo / "app/a.txt").write_text("old\n")
    commit_all(repo, "app")
    (repo / "app/a.txt").write_text("new\n")
    [d] = box_diffs(str(repo / "app"), ["a.txt"])
    assert d["status"] == "modified" and "-old" in d["diff"] and "+new" in d["diff"]


def test_a_repository_without_commits_shows_new_files(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    (tmp_path / "a.ts").write_text("x\n")
    git(tmp_path, "add", "a.ts")
    [d] = box_diffs(str(tmp_path), ["a.ts"])
    assert d["status"] == "added" and "+x" in d["diff"]


def test_large_and_binary_files_are_not_read(repo: Path) -> None:
    (repo / "big.txt").write_text("y" * (MAX_FILE_BYTES + 1))
    (repo / "img.bin").write_bytes(b"\x89PNG\0\0data")
    out = by_path(repo, ["big.txt", "img.bin"])
    assert (out["big.txt"]["status"], out["img.bin"]["status"]) == ("too_large", "binary")


def test_long_diffs_are_cut(repo: Path) -> None:
    (repo / "long.txt").write_text("y\n" * MAX_DIFF_CHARS)
    [d] = box_diffs(str(repo), ["long.txt"])
    assert d["truncated"] is True and len(d["diff"]) <= MAX_DIFF_CHARS


def test_api_diff_uses_the_files_the_box_wrote(repo: Path, store: CanvasStore) -> None:
    (repo / "src/cart.ts").write_text("export const a = 4\n")
    doc = store.draw("local-abc", "Shop", str(repo), [Box(id="cart", label="Cart", written=["src/cart.ts"])])
    api = CanvasApi(CanvasRuntime(store))
    out = api.diff(doc.id, "cart")
    assert [f["path"] for f in out["files"]] == ["src/cart.ts"]
    assert out["files"][0]["status"] == "modified"
    with pytest.raises(CanvasError, match="no box"):
        api.diff(doc.id, "nope")
    with pytest.raises(CanvasError, match="no canvas"):
        api.diff("cv-0000000000", "cart")


def test_a_promisor_remote_cannot_run_a_command(repo: Path) -> None:
    marker = repo.parent / "lazy-fetch-ran"
    (repo / "x.txt").write_text("old\n")
    commit_all(repo, "x")
    blob = subprocess.run(
        ["git", "rev-parse", "HEAD:x.txt"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / ".git/objects" / blob[:2] / blob[2:]).unlink()
    git(repo, "config", "extensions.partialClone", "origin")
    git(repo, "config", "remote.origin.promisor", "true")
    git(repo, "config", "remote.origin.url", f"ext::sh -c touch% {marker}")
    git(repo, "config", "protocol.ext.allow", "always")
    box_diffs(str(repo), ["x.txt"])
    assert not marker.exists()
