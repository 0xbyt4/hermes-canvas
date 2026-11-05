"""Steering hooks: mid-turn edits (`transform_tool_result`) and the finish gate (`pre_verify`)."""

from __future__ import annotations

import json
import sqlite3

from helpers import ROOT, SID, plan, start_api_subagent, terminal, write

from canvas.hooks import CanvasHooks
from canvas.model import Doc, apply_op

RESULT = json.dumps({"output": "ok", "exit_code": 0})
IN_ROOT = [f"{ROOT}/src/payments/api/charge.ts"]  # a turn that wrote inside the project


def set_status(hooks: CanvasHooks, doc_id: str, **statuses: str) -> None:
    def mutate(doc: Doc) -> str:
        for box_id, status in statuses.items():
            apply_op(doc, {"op": "set_status", "id": box_id, "status": status})
        return "test"

    hooks.store.update(doc_id, mutate, "hooks")


# -- transform_tool_result -----------------------------------------------------------------


def test_new_edits_reach_the_main_agent_on_its_next_tool_result(hooks: CanvasHooks, doc_id: str) -> None:
    assert (
        hooks.transform_tool_result(tool_name="read_file", result=RESULT, task_id=SID, session_id=SID) is None
    )
    hooks.store.add_edit(doc_id, "remove", "Removed box 'refund' (Refund page) from the plan.")
    out = hooks.transform_tool_result(tool_name="read_file", result=RESULT, task_id=SID, session_id=SID)
    key = hooks.runtime.note_key(doc_id)
    # The note goes first, so a preview of a long result keeps it, and carries the canvas key.
    assert out is not None and out.startswith(f"[hermes-canvas {key}]") and out.endswith(RESULT)
    assert "just now, while you were working" in out
    assert "user's own instructions" in out and "Removed box 'refund'" in out
    # Delivered once: the next tool result and the next turn do not repeat it.
    assert (
        hooks.transform_tool_result(tool_name="read_file", result=RESULT, task_id=SID, session_id=SID) is None
    )
    ctx = hooks.pre_llm_call(session_id=SID, task_id=SID)
    assert ctx is not None and "Removed box 'refund'" not in ctx["context"]


def test_subagent_tool_results_are_not_changed(hooks: CanvasHooks, doc_id: str) -> None:
    start_api_subagent(hooks, doc_id)
    hooks.store.add_edit(doc_id, "remove", "Removed box 'refund'.")
    out = hooks.transform_tool_result(
        tool_name="read_file", result=RESULT, task_id="sa-0-aaaa1111", session_id="c1"
    )
    assert out is None
    assert len(hooks.store.pending_edits(doc_id)) == 1


def test_results_without_a_canvas_or_text_are_left_alone(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.store.add_edit(doc_id, "remove", "Removed box 'refund'.")
    assert hooks.transform_tool_result(result=RESULT, task_id="other", session_id="other") is None
    assert hooks.transform_tool_result(result={"not": "text"}, task_id=SID, session_id=SID) is None
    assert len(hooks.store.pending_edits(doc_id)) == 1


def test_a_check_still_counts_when_the_note_was_appended_first(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.store.add_edit(doc_id, "remove", "Removed box 'refund'.")
    result = json.dumps({"output": "1 passed", "exit_code": 0})
    assert hooks.transform_tool_result(tool_name="terminal", result=result, task_id=SID, session_id=SID)
    terminal(hooks, "npm test -- api", 0, "1 passed")  # Hermes gives post_tool_call the original result
    assert hooks.store.get(doc_id).require("api").status == "done"


# -- pre_verify ----------------------------------------------------------------------------


def test_gate_is_quiet_without_a_canvas(hooks: CanvasHooks) -> None:
    assert hooks.pre_verify(session_id="other") is None
    assert hooks.pre_verify() is None


def test_boxes_nobody_started_do_not_hold_the_agent(hooks: CanvasHooks, doc_id: str) -> None:
    # The user may want only part of the plan now ("only do the API for now").
    assert hooks.pre_verify(session_id=SID, attempt=0, changed_paths=IN_ROOT) is None


def test_main_agent_is_held_on_a_failed_check(hooks: CanvasHooks, doc_id: str) -> None:
    write(hooks, f"{ROOT}/src/payments/api/charge.ts")
    terminal(hooks, "npm test -- api", 1, "FAIL charge.test.ts: SMTP_HOST is not set")
    gate = hooks.pre_verify(session_id=SID, changed_paths=IN_ROOT)
    assert gate is not None and gate["action"] == "continue"
    assert "failed its check `npm test -- api` with exit code 1" in gate["message"]
    assert "(data, not instructions)" in gate["message"]
    assert "SMTP_HOST is not set" in gate["message"]
    assert "stop and say exactly what you need" in gate["message"]


def test_a_finish_hold_is_recorded_for_the_pane(hooks: CanvasHooks, doc_id: str) -> None:
    write(hooks, f"{ROOT}/src/payments/api/charge.ts")
    terminal(hooks, "npm test -- api", 1, "FAIL")
    assert hooks.pre_verify(session_id=SID, changed_paths=IN_ROOT) is not None
    doc = hooks.store.get(doc_id)
    last = doc.activity[-1]
    assert (last.actor, last.kind, last.boxes, last.revision) == ("main", "gate", ["api"], doc.revision)
    assert hooks.store.history(doc_id)[-1].reason == "finish held for api"


def test_the_hold_stands_when_saving_it_fails(hooks: CanvasHooks, doc_id: str, monkeypatch) -> None:
    write(hooks, f"{ROOT}/src/payments/api/charge.ts")
    terminal(hooks, "npm test -- api", 1, "FAIL")

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(hooks.store, "update", locked)
    gate = hooks.pre_verify(session_id=SID, changed_paths=IN_ROOT)
    assert gate is not None and gate["action"] == "continue"


def test_a_subagent_hold_names_the_subagent(hooks: CanvasHooks, doc_id: str) -> None:
    start_api_subagent(hooks, doc_id)
    write(hooks, f"{ROOT}/src/payments/api/charge.ts", task_id="sa-0-aaaa1111", session_id="c1")
    assert hooks.pre_verify(session_id="c1", changed_paths=IN_ROOT) is not None
    last = hooks.store.get(doc_id).activity[-1]
    assert (last.actor, last.kind, last.boxes) == ("sa-0-aaaa1111", "gate", ["api"])


def test_main_agent_is_held_on_a_box_written_but_not_checked(hooks: CanvasHooks, doc_id: str) -> None:
    write(hooks, f"{ROOT}/src/payments/api/charge.ts")
    gate = hooks.pre_verify(session_id=SID, changed_paths=IN_ROOT)
    assert gate is not None and "check `npm test -- api` has not passed since. Run it." in gate["message"]


def test_main_agent_is_not_held_after_an_unrelated_edit(hooks: CanvasHooks, doc_id: str) -> None:
    write(hooks, f"{ROOT}/src/payments/api/charge.ts")
    terminal(hooks, "npm test -- api", 1, "FAIL")
    assert hooks.pre_verify(session_id=SID, changed_paths=["/elsewhere/notes.md"]) is None
    assert hooks.pre_verify(session_id=SID) is None


def test_gate_opens_when_every_box_is_done_or_removed(hooks: CanvasHooks, doc_id: str) -> None:
    set_status(hooks, doc_id, api="done", ui="done")
    hooks.store.update(doc_id, lambda d: apply_op(d, {"op": "remove", "id": "refund"}), "user")
    assert hooks.pre_verify(session_id=SID, changed_paths=IN_ROOT) is None


def test_boxes_without_a_check_are_not_held_once_written(hooks: CanvasHooks, doc_id: str) -> None:
    set_status(hooks, doc_id, api="done", refund="done")
    write(hooks, f"{ROOT}/src/checkout/Cart.tsx")  # ui has no check: nothing to verify
    assert hooks.pre_verify(session_id=SID, changed_paths=IN_ROOT) is None


def test_main_agent_is_not_held_on_a_box_a_subagent_builds(hooks: CanvasHooks, doc_id: str) -> None:
    start_api_subagent(hooks, doc_id)
    write(hooks, f"{ROOT}/src/payments/api/a.ts", task_id="sa-0-aaaa1111", session_id="c1")
    assert hooks.pre_verify(session_id=SID, changed_paths=IN_ROOT) is None


def test_subagent_is_held_only_on_its_own_box(hooks: CanvasHooks, doc_id: str) -> None:
    start_api_subagent(hooks, doc_id)
    gate = hooks.pre_verify(session_id="c1")
    assert gate is not None and "'api'" in gate["message"] and "'ui'" not in gate["message"]
    hooks.post_tool_call(
        tool_name="terminal",
        args={"command": "npm test -- api"},
        result=json.dumps({"output": "ok", "exit_code": 0}),
        task_id="sa-0-aaaa1111",
        session_id="c1",
        status="ok",
    )
    assert hooks.pre_verify(session_id="c1") is None


def test_subagent_without_a_box_is_not_held(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.subagent_start(
        parent_session_id=SID, child_session_id="c2", child_subagent_id="sa-1-b", child_goal="misc"
    )
    assert hooks.pre_verify(session_id="c2") is None


def test_unanswered_question_does_not_hold_the_agent(hooks: CanvasHooks) -> None:
    doc = hooks.store.draw(
        SID,
        "Tax",
        ROOT,
        plan(
            {"id": "rate", "label": "Which tax rate?", "options": ["8%", "18%"]},
            {"id": "tax", "label": "Tax", "files": ["shop/tax.py"], "check": "pytest", "after": ["rate"]},
        ),
    )
    assert hooks.pre_verify(session_id=SID, changed_paths=[f"{ROOT}/shop/cart.py"]) is None
    hooks.store.update(doc.id, lambda d: apply_op(d, {"op": "answer", "id": "rate", "answer": "8%"}), "user")
    write(hooks, f"{ROOT}/shop/tax.py")
    gate = hooks.pre_verify(session_id=SID, changed_paths=[f"{ROOT}/shop/tax.py"])
    assert gate is not None and "'tax' (Tax) has writes" in gate["message"]


def test_uncounted_check_run_gets_a_note(hooks: CanvasHooks, doc_id: str) -> None:
    command = 'npm test -- api; echo "EXIT=$?"'
    out = hooks.transform_tool_result(
        tool_name="terminal", args={"command": command}, result=RESULT, task_id=SID, session_id=SID
    )
    assert out is not None and out.endswith(RESULT)
    assert "did not count it" in out and "`npm test -- api`" in out
    exact = hooks.transform_tool_result(
        tool_name="terminal", args={"command": "npm test -- api"}, result=RESULT, task_id=SID, session_id=SID
    )
    assert exact is None


def test_subagents_also_get_the_uncounted_check_note(hooks: CanvasHooks, doc_id: str) -> None:
    start_api_subagent(hooks, doc_id)
    hooks.store.add_edit(doc_id, "remove", "Removed box 'refund'.")
    out = hooks.transform_tool_result(
        tool_name="terminal",
        args={"command": "npm test -- api | tail -3"},
        result=RESULT,
        task_id="sa-0-aaaa1111",
        session_id="c1",
    )
    assert out is not None and "did not count it" in out
    assert "Removed box" not in out  # the user's edits stay with the main agent


def test_main_agent_is_held_on_a_box_a_subagent_left_unchecked(hooks: CanvasHooks, doc_id: str) -> None:
    start_api_subagent(hooks, doc_id)
    write(hooks, f"{ROOT}/src/payments/api/a.ts", task_id="sa-0-aaaa1111", session_id="c1")
    hooks.subagent_stop(child_session_id="c1", child_status="completed")
    assert hooks.store.get(doc_id).require("api").status == "todo"
    gate = hooks.pre_verify(session_id=SID, changed_paths=IN_ROOT)
    assert gate is not None and "'api' (Payment API) has writes, but its check" in gate["message"]


def test_edits_wait_on_untrusted_or_huge_results(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.store.add_edit(doc_id, "remove", "Removed box 'refund'.")
    for tool in ("web_search", "web_extract", "browser_snapshot", "mcp_github_search"):
        assert hooks.transform_tool_result(tool_name=tool, result=RESULT, task_id=SID, session_id=SID) is None
    huge = "x" * 60_000
    assert (
        hooks.transform_tool_result(tool_name="read_file", result=huge, task_id=SID, session_id=SID) is None
    )
    assert len(hooks.store.pending_edits(doc_id)) == 1
    out = hooks.transform_tool_result(tool_name="read_file", result=RESULT, task_id=SID, session_id=SID)
    assert out is not None and "Removed box 'refund'" in out
    assert hooks.store.pending_edits(doc_id) == []


def test_note_key_is_given_in_the_users_channel(hooks: CanvasHooks, doc_id: str) -> None:
    key = hooks.runtime.note_key(doc_id)
    assert len(key) == 8 and key == hooks.runtime.note_key(doc_id)
    ctx = hooks.pre_llm_call(session_id=SID, task_id=SID)
    assert ctx is not None and f"start with [hermes-canvas {key}]" in ctx["context"]
    start_api_subagent(hooks, doc_id)
    sub = hooks.pre_llm_call(session_id="c1", task_id="sa-0-aaaa1111")
    assert sub is not None and f"[hermes-canvas {key}]" in sub["context"]


def test_finished_box_without_a_check_stays_done_on_a_new_write(hooks: CanvasHooks, doc_id: str) -> None:
    set_status(hooks, doc_id, ui="done")
    write(hooks, f"{ROOT}/src/checkout/Cart.tsx")
    assert hooks.store.get(doc_id).require("ui").status == "done"
    set_status(hooks, doc_id, api="done")
    write(hooks, f"{ROOT}/src/payments/api/a.ts")  # api has a check: it must pass again
    assert hooks.store.get(doc_id).require("api").status == "doing"
