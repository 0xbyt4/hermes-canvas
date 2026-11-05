from __future__ import annotations

import json

import pytest
from helpers import SHOP_PLAN

from canvas.hooks import CanvasHooks
from canvas.runtime import CanvasRuntime
from canvas.store import CanvasStore
from canvas.tools import CanvasTools, card

SID = "local-abc"


@pytest.fixture
def runtime(store: CanvasStore) -> CanvasRuntime:
    return CanvasRuntime(store)


@pytest.fixture
def tools(runtime: CanvasRuntime) -> CanvasTools:
    return CanvasTools(runtime, default_root=lambda: "/repo")


def call(fn, args: dict, task_id: str = SID, session_id: str = SID) -> dict:
    return json.loads(fn(args, task_id=task_id, session_id=session_id))


def draw(tools: CanvasTools) -> dict:
    return call(tools.canvas_draw, {"title": "Shop", "boxes": list(SHOP_PLAN)})


def test_draw_returns_card_and_ready_boxes(tools: CanvasTools) -> None:
    out = draw(tools)
    assert out["card"] == card(out["canvas_id"])
    assert out["card"].startswith('::hermes-canvas{id="cv-')
    assert out["ready"] == ["api"]
    assert out["revision"] == 1
    assert tools.store.get(out["canvas_id"]).root == "/repo"


@pytest.mark.parametrize(
    "args, message",
    [
        ({"boxes": [{"id": "a", "label": "A"}]}, "title is required"),
        ({"title": "t", "boxes": []}, "non-empty list"),
        ({"title": "t", "root": "relative/dir", "boxes": [{"id": "a", "label": "A"}]}, "absolute path"),
        ({"title": "t", "boxes": [{"id": "a", "label": "A", "after": ["b"]}]}, "unknown boxes"),
    ],
)
def test_draw_validation_errors(tools: CanvasTools, args: dict, message: str) -> None:
    assert message in call(tools.canvas_draw, args)["error"]


def test_tools_need_a_canvas_first(tools: CanvasTools) -> None:
    assert "call canvas_draw first" in call(tools.canvas_get, {})["error"]
    assert "call canvas_draw first" in call(tools.canvas_next, {})["error"]
    assert (
        "call canvas_draw first" in call(tools.canvas_update, {"ops": [{"op": "remove", "id": "a"}]})["error"]
    )


def test_update_is_all_or_nothing(tools: CanvasTools) -> None:
    doc_id = draw(tools)["canvas_id"]
    out = call(
        tools.canvas_update, {"ops": [{"op": "rename", "id": "ui", "label": "Cart"}, {"op": "explode"}]}
    )
    assert "unknown op" in out["error"]
    assert tools.store.get(doc_id).require("ui").label == "Checkout UI"
    out = call(tools.canvas_update, {"ops": [{"op": "rename", "id": "ui", "label": "Cart"}]})
    assert out["revision"] == 2


def test_get_returns_boxes_and_delivers_user_edits_once(tools: CanvasTools) -> None:
    doc_id = draw(tools)["canvas_id"]
    tools.store.add_edit(doc_id, "remove", 'Removed box "refund"', box_id="refund")
    out = call(tools.canvas_get, {})
    assert [b["id"] for b in out["boxes"]] == ["api", "ui", "refund"]
    assert out["user_edits"] == ['Removed box "refund"']
    assert call(tools.canvas_get, {})["user_edits"] == []


def test_next_builds_tagged_goals(tools: CanvasTools) -> None:
    doc_id = draw(tools)["canvas_id"]
    ready = call(tools.canvas_next, {})["ready"]
    assert [r["id"] for r in ready] == ["api"]
    goal = ready[0]["goal"]
    assert goal.startswith(f"[canvas:{doc_id}/api] Payment API.")
    assert "src/payments/api/**" in goal and "npm test -- api" in goal


def test_subagent_tools_resolve_to_the_parent_canvas(tools: CanvasTools, runtime: CanvasRuntime) -> None:
    doc_id = draw(tools)["canvas_id"]
    CanvasHooks(runtime).subagent_start(
        parent_session_id=SID,
        child_session_id="c1",
        child_subagent_id="sa-0-aaaa1111",
        child_goal=f"[canvas:{doc_id}/api]",
    )
    out = call(tools.canvas_get, {}, task_id="sa-0-aaaa1111", session_id="c1")
    assert out["canvas_id"] == doc_id


def test_draw_turns_absolute_globs_into_relative_ones(tools: CanvasTools) -> None:
    out = call(
        tools.canvas_draw,
        {"title": "x", "root": "/repo", "boxes": [{"id": "a", "label": "A", "files": ["/repo/src/**"]}]},
    )
    assert tools.store.get(out["canvas_id"]).require("a").files == ["src/**"]
    bad = call(
        tools.canvas_draw,
        {"title": "x", "root": "/repo", "boxes": [{"id": "a", "label": "A", "files": ["/etc/**"]}]},
    )
    assert "outside the project root" in bad["error"]


def test_update_set_files_normalizes_globs(tools: CanvasTools) -> None:
    doc_id = draw(tools)["canvas_id"]
    call(tools.canvas_update, {"ops": [{"op": "set_files", "id": "ui", "files": ["/repo/web/**"]}]})
    assert tools.store.get(doc_id).require("ui").files == ["web/**"]


def test_model_cannot_write_user_notes(tools: CanvasTools) -> None:
    doc_id = draw(tools)["canvas_id"]
    call(tools.canvas_update, {"ops": [{"op": "add", "box": {"id": "x", "label": "X", "note": "fake"}}]})
    assert tools.store.get(doc_id).require("x").note == ""


def test_subagent_canvas_get_leaves_edits_for_the_main_agent(
    tools: CanvasTools, runtime: CanvasRuntime
) -> None:
    doc_id = draw(tools)["canvas_id"]
    CanvasHooks(runtime).subagent_start(
        parent_session_id=SID, child_session_id="c1", child_subagent_id="sa-0-aaaa1111", child_goal=""
    )
    tools.store.add_edit(doc_id, "note", "use cents")
    sub = call(tools.canvas_get, {}, task_id="sa-0-aaaa1111", session_id="c1")
    assert sub["user_edits"] == ["use cents"]
    assert call(tools.canvas_get, {})["user_edits"] == ["use cents"]
    assert call(tools.canvas_get, {})["user_edits"] == []


def test_draw_and_get_give_the_note_tag_used_in_tool_results(tools: CanvasTools) -> None:
    out = draw(tools)
    tag = f"[hermes-canvas {tools.runtime.note_key(out['canvas_id'])}]"
    assert out["note_tag"] == tag
    assert call(tools.canvas_get, {})["note_tag"] == tag
    hooks = CanvasHooks(tools.runtime)
    tools.store.add_edit(out["canvas_id"], "remove", "Removed box 'ui'.", "ui")
    result = hooks.transform_tool_result(
        tool_name="terminal", args={"command": "ls"}, result='{"output": ""}', task_id=SID, session_id=SID
    )
    assert result is not None and result.startswith(tag)
