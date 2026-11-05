from __future__ import annotations

import pytest
from helpers import SHOP_PLAN, plan

from canvas.api import CanvasApi, broadcaster
from canvas.hooks import CanvasHooks
from canvas.model import CanvasError, Doc, apply_op
from canvas.runtime import CanvasRuntime
from canvas.store import CanvasStore

SID = "local-abc"


@pytest.fixture
def api(store: CanvasStore) -> CanvasApi:
    return CanvasApi(CanvasRuntime(store))


@pytest.fixture
def doc_id(api: CanvasApi) -> str:
    return api.store.draw(SID, "Shop", "/repo", plan(*SHOP_PLAN)).id


def test_canvas_by_session_and_by_id(api: CanvasApi, doc_id: str) -> None:
    by_session = api.canvas(session_id=SID)["canvas"]
    assert by_session["id"] == doc_id
    assert [b["id"] for b in by_session["boxes"]] == ["api", "ui", "refund"]
    assert by_session["ready"] == ["api"]
    assert api.canvas(canvas_id=doc_id)["canvas"]["revision"] == 1
    assert api.canvas(session_id="other") == {"canvas": None}
    assert api.canvas() == {"canvas": None}


def test_poll_with_since_returns_only_changes(api: CanvasApi, doc_id: str) -> None:
    assert api.canvas(session_id=SID, since=1) == {"unchanged": True, "revision": 1, "pending_edits": 0}
    api.edit(doc_id, [{"op": "move", "id": "api", "x": 10, "y": 20}])
    assert api.canvas(session_id=SID, since=1)["canvas"]["revision"] == 2


def test_user_edits_are_saved_as_user_and_queued_for_the_agent(api: CanvasApi, doc_id: str) -> None:
    out = api.edit(
        doc_id,
        [
            {"op": "remove", "id": "refund"},
            {"op": "rename", "id": "ui", "label": "Checkout page"},
            {"op": "move", "id": "api", "x": 100, "y": 40},
        ],
    )
    assert out["pending_edits"] == 2
    boxes = {b["id"]: b for b in out["canvas"]["boxes"]}
    assert boxes["refund"]["removed"] is True
    assert (boxes["api"]["x"], boxes["api"]["y"]) == (100.0, 40.0)
    assert api.store.history(doc_id)[-1].author == "user"
    texts = [e.text for e in api.store.pending_edits(doc_id)]
    assert texts[0].startswith("Removed box 'refund' (Refund page) from the plan.")
    assert "from 'Checkout UI' to 'Checkout page'" in texts[1]


def test_agent_reads_user_edits_once(api: CanvasApi, doc_id: str) -> None:
    api.edit(doc_id, [{"op": "remove", "id": "refund"}])
    hooks = CanvasHooks(api.runtime)
    first = hooks.pre_llm_call(session_id=SID, task_id=SID)
    assert first is not None and "Removed box 'refund'" in first["context"]
    assert "user's own instructions" in first["context"]
    second = hooks.pre_llm_call(session_id=SID, task_id=SID)
    assert second is not None and "Removed box 'refund'" not in second["context"]


def test_removal_from_the_pane_blocks_the_next_write(api: CanvasApi, doc_id: str) -> None:
    hooks = CanvasHooks(api.runtime)
    args = {"path": "src/refund/page.tsx"}
    assert hooks.pre_tool_call(tool_name="write_file", args=args, task_id=SID, session_id=SID) is None
    api.edit(doc_id, [{"op": "remove", "id": "refund"}])
    decision = hooks.pre_tool_call(tool_name="write_file", args=args, task_id=SID, session_id=SID)
    assert decision is not None and decision["action"] == "block"


def test_model_positions_are_ignored_and_user_moves_survive_a_redraw(api: CanvasApi) -> None:
    placed = {"id": "api", "label": "Refund API", "x": 900, "y": 900}
    doc = api.store.draw(SID, "Shop", "/repo", plan(placed))
    assert (doc.require("api").x, doc.require("api").y) == (None, None)
    api.edit(doc.id, [{"op": "move", "id": "api", "x": 40, "y": 60}])
    redrawn = api.store.draw(SID, "Shop", "/repo", plan(placed))
    assert (redrawn.require("api").x, redrawn.require("api").y) == (40.0, 60.0)


def test_answer_question(api: CanvasApi) -> None:
    doc = api.store.draw(
        SID, "Pay", "/repo", plan({"id": "q", "label": "Which provider?", "options": ["Stripe", "iyzico"]})
    )
    out = api.edit(doc.id, [{"op": "answer", "id": "q", "answer": "Stripe"}])
    q = out["canvas"]["boxes"][0]
    assert (q["answer"], q["status"]) == ("Stripe", "done")
    assert api.store.pending_edits(doc.id)[0].text == (
        "Answered the question 'q' (Which provider?): Stripe. Build with this choice."
    )


@pytest.mark.parametrize(
    "ops, message",
    [
        ([], "non-empty"),
        ("remove", "non-empty"),
        ([{"op": "set_check", "id": "api", "check": "rm -rf /"}], "not available on the canvas"),
        ([{"op": "add", "box": {"id": "x", "label": "X"}}], "not available on the canvas"),
        ([{"op": "remove", "id": "nope"}], "no box"),
        ([{"op": "move", "id": "api"}], "must be a number"),
        ([{"op": "move", "id": "api", "x": float("inf"), "y": 0}], "finite"),
        ([{"op": "move", "id": "api", "x": float("nan"), "y": 0}], "finite"),
        ([{"op": "move", "id": "api", "x": 0, "y": 1e7}], "finite"),
        ([{"op": "move", "id": "api", "x": 1, "y": 1}] * 21, "at most 20"),
    ],
)
def test_invalid_edits_change_nothing(api: CanvasApi, doc_id: str, ops: object, message: str) -> None:
    with pytest.raises(CanvasError, match=message):
        api.edit(doc_id, ops)
    assert api.store.get(doc_id).revision == 1
    assert api.store.pending_edits(doc_id) == []


def test_edit_on_unknown_canvas(api: CanvasApi) -> None:
    with pytest.raises(CanvasError, match="no canvas"):
        api.edit("cv-0000000000", [{"op": "remove", "id": "api"}])


def test_history_and_revision(api: CanvasApi, doc_id: str) -> None:
    api.edit(doc_id, [{"op": "remove", "id": "refund"}])
    history = api.history(doc_id)["revisions"]
    assert [(h["revision"], h["author"]) for h in history] == [(1, "agent"), (2, "user")]
    first = api.revision(doc_id, 1)["canvas"]
    assert next(b for b in first["boxes"] if b["id"] == "refund")["removed"] is False
    with pytest.raises(CanvasError):
        api.revision(doc_id, 9)


def test_broadcaster_sends_the_session_in_the_payload(store: CanvasStore) -> None:
    sent: list[tuple[str, dict]] = []
    store.on_change(broadcaster(lambda event, payload: sent.append((event, payload))))
    doc = store.draw(SID, "Shop", "/repo", plan(*SHOP_PLAN))
    assert sent == [
        (
            "changed",
            {
                "session_id": SID,
                "canvas_id": doc.id,
                "revision": 1,
                "author": "agent",
                "reason": "drew the plan",
            },
        )
    ]


def test_broadcast_failure_does_not_break_saving(store: CanvasStore) -> None:
    def boom(event: str, payload: dict) -> None:
        raise RuntimeError("no gateway")

    store.on_change(broadcaster(boom))
    doc: Doc = store.draw(SID, "Shop", "/repo", plan(*SHOP_PLAN))
    assert store.get(doc.id) is not None


def test_unchanged_poll_reports_the_pending_count(api: CanvasApi, doc_id: str) -> None:
    api.edit(doc_id, [{"op": "remove", "id": "refund"}])
    assert api.canvas(session_id=SID, since=2)["pending_edits"] == 1
    CanvasHooks(api.runtime).pre_llm_call(session_id=SID, task_id=SID)  # the agent takes the edit
    assert api.canvas(session_id=SID, since=2) == {"unchanged": True, "revision": 2, "pending_edits": 0}


def test_repeated_remove_or_restore_queues_one_note(api: CanvasApi, doc_id: str) -> None:
    api.edit(doc_id, [{"op": "remove", "id": "refund"}])
    api.edit(doc_id, [{"op": "remove", "id": "refund"}])
    api.edit(doc_id, [{"op": "restore", "id": "refund"}])
    api.edit(doc_id, [{"op": "restore", "id": "refund"}])
    assert [e.kind for e in api.store.pending_edits(doc_id)] == ["remove", "restore"]


def test_restoring_a_finished_box_does_not_ask_for_a_rebuild(api: CanvasApi, doc_id: str) -> None:
    done = {"op": "set_status", "id": "api", "status": "done"}
    api.store.update(doc_id, lambda d: apply_op(d, done), "hooks")
    api.edit(doc_id, [{"op": "remove", "id": "api"}])
    api.edit(doc_id, [{"op": "restore", "id": "api"}])
    note = api.store.pending_edits(doc_id)[-1].text
    assert "already done" in note and "do not rebuild" in note


def test_edit_note_is_saved_with_the_revision(api: CanvasApi, doc_id: str) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("notes must not be written in a second step")

    api.store.add_edit = fail  # type: ignore[method-assign]  # prove the edit path does not use it
    api.edit(doc_id, [{"op": "remove", "id": "refund"}])
    edits = api.store.pending_edits(doc_id)
    assert [e.kind for e in edits] == ["remove"]
    assert edits[0].created_at == api.store.get(doc_id).updated_at
