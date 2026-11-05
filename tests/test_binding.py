from __future__ import annotations

import pytest
from helpers import SHOP_PLAN, plan

from canvas.binding import (
    Binding,
    glob_to_regex,
    goal_tag,
    normalize_command,
    normalize_globs,
    parse_goal_tag,
    paths_from_args,
    paths_from_result,
    relativize,
)
from canvas.model import Box, CanvasError, Doc


@pytest.mark.parametrize(
    "pattern, path, expected",
    [
        ("src/payments/api/**", "src/payments/api/index.ts", True),
        ("src/payments/api/**", "src/payments/api/v1/charge.ts", True),
        ("src/payments/api/**", "src/payments/apix/a.ts", False),
        ("src/checkout/*.tsx", "src/checkout/Cart.tsx", True),
        ("src/checkout/*.tsx", "src/checkout/deep/Cart.tsx", False),
        ("**/*.test.ts", "money.test.ts", True),
        ("**/*.test.ts", "src/a/money.test.ts", True),
        ("src/email/", "src/email/send.ts", True),
        ("./src/a.ts", "src/a.ts", True),
        (".github/workflows/*.yml", ".github/workflows/ci.yml", True),
        ("src/?.ts", "src/a.ts", True),
        ("src/?.ts", "src/ab.ts", False),
        ("src/a+b.ts", "src/a+b.ts", True),
        ("src/*.{ts,tsx}", "src/a.tsx", True),
        ("src/*.{ts,tsx}", "src/a.js", False),
        ("src/payments", "src/payments/api/a.ts", True),
        ("src/payments", "src/payments", True),
        ("src/payments", "src/paymentsx/a.ts", False),
        ("src/{api,ui}", "src/api/x.ts", True),
        ("src/{api,ui}", "src/ui", True),
    ],
)
def test_glob_to_regex(pattern: str, path: str, expected: bool) -> None:
    assert bool(glob_to_regex(pattern).match(path)) is expected


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/repo/src/a.ts", "src/a.ts"),
        ("src/a.ts", "src/a.ts"),
        ("/repo/src/../lib/b.ts", "lib/b.ts"),
        ("/other/src/a.ts", None),
        ("../outside.ts", None),
        ("/repo", None),
        ("", None),
    ],
)
def test_relativize(path: str, expected: str | None) -> None:
    assert relativize(path, "/repo") == expected


def test_paths_from_args() -> None:
    assert paths_from_args("write_file", {"path": "src/a.ts", "content": "x"}) == ["src/a.ts"]
    assert paths_from_args("patch", {"path": "src/a.ts", "old_string": "a", "new_string": "b"}) == [
        "src/a.ts"
    ]
    v4a = (
        "*** Begin Patch\n*** Update File: src/a.ts\n@@\n-a\n+b\n"
        "*** Add File: src/new.ts\n+x\n*** Delete File: src/old.ts\n"
        "*** Update File: src/c.ts\n*** Move to: src/d.ts\n*** End Patch"
    )
    assert paths_from_args("patch", {"mode": "patch", "patch": v4a}) == [
        "src/a.ts",
        "src/new.ts",
        "src/old.ts",
        "src/c.ts",
        "src/d.ts",
    ]
    assert paths_from_args("read_file", {"path": "src/a.ts"}) == []
    assert paths_from_args("write_file", "not a dict") == []


def test_normalize_command() -> None:
    assert normalize_command("  npm   test --  api ") == "npm test -- api"
    assert normalize_command("cd /repo && npm test -- api") == "npm test -- api"
    assert normalize_command("cd '/my repo'; npm test") == "npm test"
    assert normalize_command("echo 'unclosed") == "echo 'unclosed"


def test_goal_tag_round_trip() -> None:
    tag = goal_tag("cv-0123456789", "api")
    assert parse_goal_tag(f"{tag} Build the payment API") == ("cv-0123456789", "api")
    assert parse_goal_tag("no tag here") is None
    assert parse_goal_tag("[canvas:other/api]") is None


def make_binding() -> Binding:
    doc = Doc(id="cv-1", session_id="s1", title="Shop", root="/repo", boxes=plan(*SHOP_PLAN))
    doc.require("refund").removed = True
    doc.boxes.append(Box.from_plan({"id": "all-tests", "label": "Tests", "check": "npm test"}))
    return Binding.build(doc)


def test_binding_paths_and_removed_boxes() -> None:
    binding = make_binding()
    assert binding.boxes_for_path("src/payments/api/charge.ts") == ["api"]
    assert binding.boxes_for_path("src/refund/page.tsx") == []
    assert binding.removed_box_for_path("src/refund/page.tsx") == "refund"
    assert binding.removed_box_for_path("src/payments/api/charge.ts") is None
    assert binding.removed_box_for_path("README.md") is None


def test_removed_box_does_not_veto_paths_an_active_box_owns() -> None:
    doc = Doc(
        id="cv-1",
        session_id="s1",
        title="t",
        root="/repo",
        boxes=plan(
            {"id": "a", "label": "A", "files": ["src/**"]}, {"id": "b", "label": "B", "files": ["src/b/**"]}
        ),
    )
    doc.require("b").removed = True
    assert Binding.build(doc).removed_box_for_path("src/b/x.ts") is None


def test_binding_checks_match_the_exact_command_only() -> None:
    binding = make_binding()
    assert binding.boxes_for_command("cd /repo && npm test -- api") == ["api"]
    assert binding.boxes_for_command("npm test -- api 2>&1") == ["api"]
    assert binding.boxes_for_command("npm test") == ["all-tests"]
    assert binding.boxes_for_command("echo 'npm test -- api'") == []
    assert binding.boxes_for_command("npm test -- api || true") == []
    assert binding.boxes_for_command("npm test -- api -t refunds") == []
    assert binding.boxes_for_command("ls -la") == []


def test_check_runs_with_extra_shell_parts_are_near_misses() -> None:
    binding = make_binding()
    api = [("api", "npm test -- api")]
    assert binding.near_checks('npm test -- api; echo "exit=$?"') == api
    assert binding.near_checks("cd /repo && npm test -- api | tail -5") == api
    assert binding.near_checks("npm test -- api || true") == api
    assert binding.near_checks("npm test -- api 2>&1 | tail") == api
    assert binding.near_checks("npm test -- api 2>/dev/null") == api
    assert binding.near_checks("npm test -- api\necho done") == api
    assert binding.near_checks("npm test -- api") == []  # exact: counted, not a near miss
    assert binding.near_checks("npm test -- api -t refunds") == []  # another command
    assert binding.near_checks("echo 'npm test -- api'") == []
    assert binding.near_checks("ls -la") == []


def test_binding_returns_every_box_that_shares_a_check() -> None:
    doc = Doc(
        id="cv-1",
        session_id="s1",
        title="t",
        root="/repo",
        boxes=plan(
            {"id": "a", "label": "A", "check": "pytest -q"},
            {"id": "b", "label": "B", "check": "pytest  -q"},
        ),
    )
    assert Binding.build(doc).boxes_for_command("pytest -q") == ["a", "b"]


def test_removed_drift_box_does_not_veto() -> None:
    doc = Doc(id="cv-1", session_id="s1", title="t", root="/repo", boxes=plan(*SHOP_PLAN))
    doc.boxes.append(Box(id="drift-1", label="Drift", kind="drift", files=["src/money.ts"], removed=True))
    assert Binding.build(doc).removed_box_for_path("src/money.ts") is None


def test_drift_box_does_not_cancel_a_removal() -> None:
    doc = Doc(
        id="cv-1",
        session_id="s1",
        title="t",
        root="/repo",
        boxes=plan({"id": "x", "label": "X", "files": ["src/a.ts"]}),
    )
    doc.boxes.append(Box(id="drift-1", label="Drift", kind="drift", files=["src/a.ts"]))
    doc.require("x").removed = True
    assert Binding.build(doc).removed_box_for_path("src/a.ts") == "x"


def test_relativize_resolves_symlinks(tmp_path) -> None:
    real = tmp_path / "real"
    (real / "src").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    assert relativize(str(real / "src" / "a.ts"), str(link)) == "src/a.ts"
    assert relativize(str(link / "src" / "a.ts"), str(real)) == "src/a.ts"


def test_normalize_globs() -> None:
    boxes = plan({"id": "a", "label": "A", "files": ["/repo/src/**", "lib/", "/repo/docs/"]})
    normalize_globs(boxes, "/repo")
    assert boxes[0].files == ["src/**", "lib/", "docs/"]
    with pytest.raises(CanvasError, match="outside the project root"):
        normalize_globs(plan({"id": "b", "label": "B", "files": ["/etc/**"]}), "/repo")


def test_paths_from_result() -> None:
    result = {
        "files_modified": ["/ws/src/a.ts"],
        "files_created": ["/ws/src/b.ts", "relative.ts"],
        "resolved_path": "/ws/src/a.ts",
    }
    assert paths_from_result(result) == ["/ws/src/a.ts", "/ws/src/b.ts"]
    assert paths_from_result({"resolved_path": "/ws/x.ts"}) == ["/ws/x.ts"]
    assert paths_from_result(None) == []
