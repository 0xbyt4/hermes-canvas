from __future__ import annotations

import pytest
from helpers import SHOP_PLAN, plan

from canvas.model import Box, CanvasError, Doc, apply_op, ready_boxes, validate_plan


def make_doc() -> Doc:
    return Doc(id="cv-1", session_id="s1", title="Shop", root="/repo", boxes=plan(*SHOP_PLAN))


def test_from_plan_infers_question_kind() -> None:
    box = Box.from_plan({"id": "q1", "label": "Which provider?", "options": ["Stripe", "Adyen"]})
    assert box.kind == "question"
    assert box.options == ["Stripe", "Adyen"]


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"label": "x"}, "id is required"),
        ({"id": "a b", "label": "x"}, "must use letters"),
        ({"id": "a", "label": ""}, "label is required"),
        ({"id": "a", "label": "x", "kind": "blob"}, "kind must be one of"),
        ({"id": "a", "label": "x", "files": [1]}, "list of strings"),
        ({"id": "a", "label": "x", "files": ["src/" + "a" * 253]}, "longer than 256"),
    ],
)
def test_from_plan_rejects_bad_input(raw: dict, message: str) -> None:
    with pytest.raises(CanvasError, match=message):
        Box.from_plan(raw)


def test_validate_plan_rejects_duplicates_unknown_deps_and_cycles() -> None:
    with pytest.raises(CanvasError, match="duplicate"):
        validate_plan(plan({"id": "a", "label": "A"}, {"id": "a", "label": "B"}))
    with pytest.raises(CanvasError, match="unknown boxes: zz"):
        validate_plan(plan({"id": "a", "label": "A", "after": ["zz"]}))
    with pytest.raises(CanvasError, match="cycle: a -> b -> a"):
        validate_plan(
            plan({"id": "a", "label": "A", "after": ["b"]}, {"id": "b", "label": "B", "after": ["a"]})
        )


def test_ready_boxes_follow_dependencies_and_ignore_removed() -> None:
    doc = make_doc()
    assert [b.id for b in ready_boxes(doc)] == ["api"]
    doc.require("api").status = "done"
    assert [b.id for b in ready_boxes(doc)] == ["ui", "refund"]
    doc.require("refund").removed = True
    assert [b.id for b in ready_boxes(doc)] == ["ui"]


def test_removed_dependency_does_not_block() -> None:
    doc = make_doc()
    doc.require("api").removed = True
    assert [b.id for b in ready_boxes(doc)] == ["ui", "refund"]


def test_apply_ops() -> None:
    doc = make_doc()
    assert apply_op(doc, {"op": "rename", "id": "ui", "label": "Checkout"}) == "renamed ui"
    apply_op(doc, {"op": "add", "box": {"id": "email", "label": "Receipt email", "after": ["api"]}})
    apply_op(doc, {"op": "set_files", "id": "email", "files": ["src/email/**"]})
    apply_op(doc, {"op": "remove", "id": "refund"})
    apply_op(doc, {"op": "move", "id": "ui", "x": 10, "y": 20})
    assert doc.require("ui").label == "Checkout"
    assert doc.require("email").files == ["src/email/**"]
    assert doc.require("refund").removed is True
    assert (doc.require("ui").x, doc.require("ui").y) == (10.0, 20.0)


def test_set_after_rejects_cycle_and_keeps_old_value() -> None:
    doc = make_doc()
    with pytest.raises(CanvasError, match="cycle"):
        apply_op(doc, {"op": "set_after", "id": "api", "after": ["ui"]})
    assert doc.require("api").after == []


def test_answer_only_for_questions() -> None:
    doc = make_doc()
    doc.boxes.append(Box.from_plan({"id": "q", "label": "Provider?", "options": ["Stripe", "Adyen"]}))
    assert apply_op(doc, {"op": "answer", "id": "q", "answer": "Stripe"}) == "answered q: Stripe"
    assert doc.require("q").status == "done"
    with pytest.raises(CanvasError, match="not a question"):
        apply_op(doc, {"op": "answer", "id": "api", "answer": "x"})


def test_set_status_cannot_pass_or_fail_a_box_with_a_check() -> None:
    doc = make_doc()
    for status in ("done", "failed"):
        with pytest.raises(CanvasError, match="run `npm test -- api` with the terminal tool"):
            apply_op(doc, {"op": "set_status", "id": "api", "status": status})
    assert doc.require("api").status == "todo"
    apply_op(doc, {"op": "set_status", "id": "api", "status": "doing"})
    # A box without a check has nothing a hook can run, so the agent may finish it by hand.
    apply_op(doc, {"op": "set_status", "id": "ui", "status": "done"})
    assert doc.require("ui").status == "done"


def test_a_check_cannot_be_removed() -> None:
    with pytest.raises(CanvasError, match="change it instead of removing it"):
        apply_op(make_doc(), {"op": "set_check", "id": "api", "check": " "})


def test_a_new_check_resets_a_finished_box_until_it_runs() -> None:
    doc = make_doc()
    apply_op(doc, {"op": "set_status", "id": "ui", "status": "done"})
    apply_op(doc, {"op": "set_check", "id": "ui", "check": "npm test -- ui"})
    assert doc.require("ui").status == "todo"
    api = doc.require("api")
    api.status, api.check_exit, api.check_output, api.writes = "failed", 1, "FAIL", 2
    apply_op(doc, {"op": "set_check", "id": "api", "check": "npm test -- payments"})
    assert (api.status, api.check_exit, api.check_output) == ("doing", None, "")
    # The same check again keeps the result it has.
    api.status = "done"
    apply_op(doc, {"op": "set_check", "id": "api", "check": "npm test -- payments"})
    assert api.status == "done"


def test_unknown_op() -> None:
    with pytest.raises(CanvasError, match="unknown op"):
        apply_op(make_doc(), {"op": "explode"})


def test_doc_round_trip() -> None:
    doc = make_doc()
    doc.require("api").check_exit = 1
    assert Doc.from_dict(doc.to_dict()) == doc
