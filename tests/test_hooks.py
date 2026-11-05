from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from helpers import ROOT, SHOP_PLAN, SID, plan, start_api_subagent, terminal, write

from canvas.hooks import BLOCK_PREFIX, CanvasHooks
from canvas.model import Doc, apply_op
from canvas.runtime import CanvasRuntime
from canvas.store import CanvasStore


def test_write_marks_box_doing_and_counts(hooks: CanvasHooks, doc_id: str) -> None:
    write(hooks, f"{ROOT}/src/payments/api/charge.ts")
    write(hooks, "src/payments/api/charge.ts")
    api = hooks.store.get(doc_id).require("api")
    assert (api.status, api.writes, api.written) == ("doing", 2, ["src/payments/api/charge.ts"])


def test_write_outside_every_box_creates_one_drift_box(hooks: CanvasHooks, doc_id: str) -> None:
    write(hooks, f"{ROOT}/src/money.ts")
    write(hooks, f"{ROOT}/src/money.ts")
    drift = [b for b in hooks.store.get(doc_id).boxes if b.kind == "drift"]
    assert len(drift) == 1
    assert (drift[0].id, drift[0].files, drift[0].writes) == ("drift-1", ["src/money.ts"], 2)


def test_writes_outside_root_and_failed_writes_are_ignored(hooks: CanvasHooks, doc_id: str) -> None:
    write(hooks, "/elsewhere/a.ts")
    hooks.post_tool_call(
        tool_name="write_file",
        args={"path": "src/payments/api/a.ts"},
        result=json.dumps({"error": "disk full"}),
        task_id=SID,
        session_id=SID,
        status="ok",
    )
    assert hooks.store.get(doc_id).revision == 1


def test_check_exit_code_sets_status(hooks: CanvasHooks, doc_id: str) -> None:
    terminal(hooks, "cd /repo && npm test -- api", 1, "FAIL charge.test.ts\nexpected 200")
    api = hooks.store.get(doc_id).require("api")
    assert (api.status, api.check_exit) == ("failed", 1)
    assert "expected 200" in api.check_output
    terminal(hooks, "npm test -- api", 0, "PASS")
    assert hooks.store.get(doc_id).require("api").status == "done"


def test_terminal_without_exit_code_or_unknown_command_is_ignored(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.post_tool_call(
        tool_name="terminal",
        args={"command": "npm test -- api", "background": True},
        result=json.dumps({"output": "started"}),
        task_id=SID,
        session_id=SID,
    )
    terminal(hooks, "ls", 0)
    assert hooks.store.get(doc_id).revision == 1


def test_check_stopped_by_approval_rule_does_not_fail_the_box(hooks: CanvasHooks, doc_id: str) -> None:
    blocked = "BLOCKED: Command flagged as dangerous (script execution via -e/-c flag) but single-query mode"
    terminal(hooks, "python3 -c 'x' && npm test -- api", -1, blocked)
    hooks.post_tool_call(
        tool_name="terminal",
        args={"command": "npm test -- api"},
        result=json.dumps({"error": "Blocked by other-plugin"}),
        task_id=SID,
        session_id=SID,
        status="blocked",
    )
    assert hooks.store.get(doc_id).revision == 1
    terminal(hooks, "npm test -- api", -9, "Killed")
    assert hooks.store.get(doc_id).require("api").status == "failed"


def test_removed_box_blocks_writes_and_counts_the_block(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.store.update(doc_id, lambda d: apply_op(d, {"op": "remove", "id": "refund"}), "user")
    decision = hooks.pre_tool_call(
        tool_name="write_file", args={"path": "src/refund/page.tsx"}, task_id=SID, session_id=SID
    )
    assert decision is not None and decision["action"] == "block"
    assert "refund" in decision["message"]
    assert (
        hooks.pre_tool_call(
            tool_name="write_file", args={"path": "src/payments/api/a.ts"}, task_id=SID, session_id=SID
        )
        is None
    )
    assert (
        hooks.pre_tool_call(
            tool_name="read_file", args={"path": "src/refund/page.tsx"}, task_id=SID, session_id=SID
        )
        is None
    )
    hooks.post_tool_call(
        tool_name="write_file",
        args={"path": "src/refund/page.tsx"},
        result=json.dumps({"error": decision["message"]}),
        task_id=SID,
        session_id=SID,
        status="blocked",
        error_message=decision["message"],
    )
    assert hooks.store.get(doc_id).require("refund").blocked == 1


def test_block_from_another_plugin_is_not_counted(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.store.update(doc_id, lambda d: apply_op(d, {"op": "remove", "id": "refund"}), "user")
    hooks.post_tool_call(
        tool_name="write_file",
        args={"path": "src/refund/page.tsx"},
        task_id=SID,
        session_id=SID,
        status="blocked",
        error_message="Blocked by other-plugin",
    )
    assert hooks.store.get(doc_id).require("refund").blocked == 0
    assert BLOCK_PREFIX.startswith("Blocked by hermes-canvas")


def test_patch_v4a_paths_are_bound(hooks: CanvasHooks, doc_id: str) -> None:
    patch = "*** Begin Patch\n*** Update File: src/checkout/Cart.tsx\n@@\n-a\n+b\n*** End Patch"
    hooks.post_tool_call(
        tool_name="patch",
        args={"mode": "patch", "patch": patch},
        result="{}",
        task_id=SID,
        session_id=SID,
        status="ok",
    )
    assert hooks.store.get(doc_id).require("ui").writes == 1


def test_sessions_without_canvas_are_ignored(hooks: CanvasHooks) -> None:
    write(hooks, "/repo/src/a.ts", task_id="other", session_id="other")
    assert (
        hooks.pre_tool_call(
            tool_name="write_file", args={"path": "/repo/a"}, task_id="other", session_id="other"
        )
        is None
    )
    assert hooks.pre_llm_call(session_id="other", task_id="other") is None


def test_subagent_with_goal_tag_owns_its_box(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.subagent_start(
        parent_session_id=SID,
        parent_subagent_id=None,
        child_session_id="20261105_1",
        child_subagent_id="sa-0-aaaa1111",
        child_goal=f"[canvas:{doc_id}/api] Payment API.",
    )
    api = hooks.store.get(doc_id).require("api")
    assert (api.owner, api.status) == ("sa-0-aaaa1111", "doing")
    write(hooks, f"{ROOT}/src/payments/api/a.ts", task_id="sa-0-aaaa1111", session_id="20261105_1")
    assert hooks.store.get(doc_id).require("api").writes == 1
    ctx = hooks.pre_llm_call(session_id="20261105_1", task_id="sa-0-aaaa1111")
    assert ctx is not None and "You build canvas box 'api'" in ctx["context"]
    hooks.subagent_stop(child_session_id="20261105_1", child_status="completed")
    assert hooks.runtime.link_for("sa-0-aaaa1111", "").finished == "completed"


def test_subagent_without_tag_still_maps_to_parent_canvas(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.subagent_start(
        parent_session_id=SID, child_session_id="c2", child_subagent_id="sa-1-bbbb2222", child_goal="do stuff"
    )
    write(hooks, f"{ROOT}/src/checkout/Cart.tsx", task_id="sa-1-bbbb2222", session_id="c2")
    assert hooks.store.get(doc_id).require("ui").writes == 1


def test_nested_subagent_inherits_parent_box(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.subagent_start(
        parent_session_id=SID,
        child_session_id="c1",
        child_subagent_id="sa-0-aaaa1111",
        child_goal=f"[canvas:{doc_id}/ui] UI",
    )
    hooks.subagent_start(
        parent_session_id="c1",
        parent_subagent_id="sa-0-aaaa1111",
        child_session_id="c3",
        child_subagent_id="sa-0-cccc3333",
    )
    assert hooks.runtime.owner_box("sa-0-cccc3333", "c3") == (doc_id, "ui")


def test_pre_llm_call_shows_plan_and_delivers_edits_once(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.store.add_edit(doc_id, "note", 'Note on "api": "Use Stripe test keys"', box_id="api")
    first = hooks.pre_llm_call(session_id=SID, task_id=SID)
    assert first is not None
    assert "api [todo] Payment API" in first["context"]
    assert "user's own instructions" in first["context"]
    assert "Use Stripe test keys" in first["context"]
    second = hooks.pre_llm_call(session_id=SID, task_id=SID)
    assert second is not None and "Use Stripe test keys" not in second["context"]


def test_pre_tool_call_is_fast(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.pre_tool_call(tool_name="write_file", args={"path": "src/a.ts"}, task_id=SID, session_id=SID)
    start = time.perf_counter()
    for _ in range(200):
        hooks.pre_tool_call(
            tool_name="write_file", args={"path": "src/payments/api/a.ts"}, task_id=SID, session_id=SID
        )
    per_call_ms = (time.perf_counter() - start) * 1000 / 200
    assert per_call_ms < 5


def test_callbacks_swallow_internal_errors(
    hooks: CanvasHooks, doc_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(hooks.runtime, "doc_id_for", boom)
    assert (
        hooks.pre_tool_call(tool_name="write_file", args={"path": "a"}, task_id=SID, session_id=SID) is None
    )
    hooks.post_tool_call(tool_name="write_file", args={"path": "a"}, task_id=SID, session_id=SID)
    assert hooks.pre_llm_call(session_id=SID, task_id=SID) is None


def test_check_results_that_never_ran_are_ignored(hooks: CanvasHooks, doc_id: str) -> None:
    terminal(hooks, "npm test -- api", -1, "waiting", status="pending_approval")
    terminal(hooks, "npm test -- api", 1, "guard", status="blocked")
    terminal(hooks, "npm test -- api", 1, "backend down", status="degraded")
    hooks.post_tool_call(
        tool_name="terminal",
        args={"command": "npm test -- api", "background": True},
        result=json.dumps({"output": "Background process started", "exit_code": 0}),
        task_id=SID,
        session_id=SID,
        status="ok",
    )
    terminal(hooks, "echo 'npm test -- api'", 0)
    assert hooks.store.get(doc_id).revision == 1


def test_shared_check_updates_every_box(hooks: CanvasHooks) -> None:
    doc = hooks.store.draw(
        SID,
        "t",
        ROOT,
        plan(
            {"id": "a", "label": "A", "check": "pytest -q"},
            {"id": "b", "label": "B", "check": "pytest -q"},
        ),
    )
    terminal(hooks, "pytest -q", 0)
    stored = hooks.store.get(doc.id)
    assert [stored.require(i).status for i in ("a", "b")] == ["done", "done"]


def test_write_uses_the_path_hermes_resolved(hooks: CanvasHooks, doc_id: str) -> None:
    # The model gave a relative path; Hermes resolved it against its workspace, not the canvas root.
    hooks.post_tool_call(
        tool_name="write_file",
        args={"path": "charge.ts"},
        result=json.dumps({"bytes_written": 1, "resolved_path": f"{ROOT}/src/payments/api/charge.ts"}),
        task_id=SID,
        session_id=SID,
        status="ok",
    )
    doc = hooks.store.get(doc_id)
    assert doc.require("api").written == ["src/payments/api/charge.ts"]
    assert not [b for b in doc.boxes if b.kind == "drift"]


def test_block_message_is_neutral(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.store.update(doc_id, lambda d: apply_op(d, {"op": "remove", "id": "refund"}), "agent")
    decision = hooks.pre_tool_call(
        tool_name="write_file", args={"path": "src/refund/a.ts"}, task_id=SID, session_id=SID
    )
    assert decision is not None
    assert "was removed from the plan" in decision["message"]
    assert "the user removed" not in decision["message"]


def test_removed_drift_box_does_not_block(hooks: CanvasHooks, doc_id: str) -> None:
    write(hooks, f"{ROOT}/src/money.ts")
    hooks.store.update(doc_id, lambda d: apply_op(d, {"op": "remove", "id": "drift-1"}), "user")
    args = {"path": "src/money.ts"}
    assert hooks.pre_tool_call(tool_name="write_file", args=args, task_id=SID, session_id=SID) is None


def test_subagent_stop_before_check_puts_the_box_back(hooks: CanvasHooks, doc_id: str) -> None:
    start_api_subagent(hooks, doc_id)
    hooks.subagent_stop(child_session_id="c1", child_status="failed")
    api = hooks.store.get(doc_id).require("api")
    assert (api.status, api.owner) == ("todo", "")
    assert "before api passed its check" in hooks.store.history(doc_id)[-1].reason


def test_subagent_stop_after_check_keeps_done(hooks: CanvasHooks, doc_id: str) -> None:
    start_api_subagent(hooks, doc_id)
    hooks.post_tool_call(
        tool_name="terminal",
        args={"command": "npm test -- api"},
        result=json.dumps({"output": "ok", "exit_code": 0}),
        task_id="sa-0-aaaa1111",
        session_id="c1",
        status="ok",
    )
    hooks.subagent_stop(child_session_id="c1", child_status="completed")
    assert hooks.store.get(doc_id).require("api").status == "done"


def test_subagents_do_not_take_the_users_edits(hooks: CanvasHooks, doc_id: str) -> None:
    start_api_subagent(hooks, doc_id)
    hooks.subagent_start(
        parent_session_id=SID, child_session_id="c2", child_subagent_id="sa-1-bbbb2222", child_goal="misc"
    )
    hooks.store.add_edit(doc_id, "note", "Use Stripe test keys")
    for task, session in (("sa-0-aaaa1111", "c1"), ("sa-1-bbbb2222", "c2")):
        ctx = hooks.pre_llm_call(session_id=session, task_id=task)
        assert ctx is not None and "Use Stripe test keys" not in ctx["context"]
    main = hooks.pre_llm_call(session_id=SID, task_id=SID)
    assert main is not None and "Use Stripe test keys" in main["context"]


def test_goal_tag_from_another_session_is_ignored(hooks: CanvasHooks, doc_id: str) -> None:
    other = hooks.store.draw("other-session", "Other", ROOT, plan(*SHOP_PLAN))
    hooks.subagent_start(
        parent_session_id=SID,
        child_session_id="c1",
        child_subagent_id="sa-0-aaaa1111",
        child_goal=f"[canvas:{other.id}/api] copied goal",
    )
    assert hooks.store.get(other.id).require("api").status == "todo"
    assert hooks.runtime.owner_box("sa-0-aaaa1111", "c1") is None
    assert hooks.runtime.doc_id_for("sa-0-aaaa1111", "c1") == doc_id


def test_drift_list_in_context_is_capped(hooks: CanvasHooks, doc_id: str) -> None:
    for i in range(8):
        write(hooks, f"{ROOT}/misc/f{i}.ts")
    ctx = hooks.pre_llm_call(session_id=SID, task_id=SID)
    assert ctx is not None
    assert "misc/f0.ts" in ctx["context"] and "misc/f4.ts" in ctx["context"]
    assert "misc/f5.ts" not in ctx["context"]
    assert "and 3 more" in ctx["context"]


def test_no_canvas_is_not_cached(hooks: CanvasHooks) -> None:
    assert hooks.pre_llm_call(session_id=SID, task_id=SID) is None
    hooks.store.draw(SID, "Shop", ROOT, plan(*SHOP_PLAN))
    assert hooks.pre_llm_call(session_id=SID, task_id=SID) is not None


def test_edit_from_another_process_reaches_pre_tool_call(tmp_path: Path) -> None:
    db = tmp_path / "data.db"
    agent = CanvasHooks(CanvasRuntime(CanvasStore(sqlite3.connect(db, check_same_thread=False))))
    desktop = CanvasStore(sqlite3.connect(db, check_same_thread=False))
    doc = agent.store.draw(SID, "Shop", ROOT, plan(*SHOP_PLAN))
    args = {"path": "src/refund/page.tsx"}
    assert agent.pre_tool_call(tool_name="write_file", args=args, task_id=SID, session_id=SID) is None
    desktop.update(doc.id, lambda d: apply_op(d, {"op": "remove", "id": "refund"}), "user")
    decision = agent.pre_tool_call(tool_name="write_file", args=args, task_id=SID, session_id=SID)
    assert decision is not None and decision["action"] == "block"


def test_subagent_without_check_is_done_when_it_completes(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.subagent_start(
        parent_session_id=SID,
        child_session_id="c1",
        child_subagent_id="sa-0-aaaa1111",
        child_goal=f"[canvas:{doc_id}/ui] UI",
    )
    hooks.subagent_stop(child_session_id="c1", child_status="completed")
    assert hooks.store.get(doc_id).require("ui").status == "done"


def test_subagent_without_check_that_fails_goes_back_to_todo(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.subagent_start(
        parent_session_id=SID,
        child_session_id="c1",
        child_subagent_id="sa-0-aaaa1111",
        child_goal=f"[canvas:{doc_id}/ui] UI",
    )
    hooks.subagent_stop(child_session_id="c1", child_status="timeout")
    assert hooks.store.get(doc_id).require("ui").status == "todo"


def test_redraw_keeps_the_users_note(hooks: CanvasHooks, doc_id: str) -> None:
    def note(d: Doc) -> str:
        d.require("api").note = "use cents"
        return "note"

    hooks.store.update(doc_id, note, "user")
    hooks.store.draw(SID, "Shop", ROOT, plan(*SHOP_PLAN))
    assert hooks.store.get(doc_id).require("api").note == "use cents"


def test_removed_box_blocks_shell_writes_too(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.store.update(doc_id, lambda d: apply_op(d, {"op": "remove", "id": "refund"}), "user")
    args = {"command": "cd /repo && mkdir -p src/refund && cat > src/refund/page.tsx <<'EOF'\nx\nEOF"}
    decision = hooks.pre_tool_call(tool_name="terminal", args=args, task_id=SID, session_id=SID)
    assert decision is not None and decision["action"] == "block"
    hooks.post_tool_call(
        tool_name="terminal",
        args=args,
        result=json.dumps({"error": decision["message"]}),
        task_id=SID,
        session_id=SID,
        status="blocked",
        error_message=decision["message"],
    )
    assert hooks.store.get(doc_id).require("refund").blocked == 1
    reading = {"command": "cat src/refund/page.tsx"}
    assert hooks.pre_tool_call(tool_name="terminal", args=reading, task_id=SID, session_id=SID) is None


def test_shell_writes_count_like_write_tools(hooks: CanvasHooks, doc_id: str) -> None:
    terminal(hooks, f"cd {ROOT}", 0)  # the shell is in the root from here on
    terminal(hooks, "cat > src/payments/api/a.ts <<'EOF'\nx\nEOF", 0)
    api = hooks.store.get(doc_id).require("api")
    assert (api.writes, api.written, api.status) == (1, ["src/payments/api/a.ts"], "doing")
    terminal(hooks, "npm test -- api", 0)
    assert hooks.store.get(doc_id).require("api").status == "done"
    terminal(hooks, "echo y > src/payments/api/b.ts && npm test -- api", 0)  # not the exact check
    api = hooks.store.get(doc_id).require("api")
    assert (api.writes, api.status) == (2, "doing")


def test_a_failing_shell_command_still_counts_its_writes(hooks: CanvasHooks, doc_id: str) -> None:
    # `cat > f < missing.txt` truncates f before the input redirect fails.
    terminal(hooks, f"cd {ROOT} && cat > src/payments/api/a.ts < missing.txt", 1)
    assert hooks.store.get(doc_id).require("api").writes == 1


def test_a_shell_command_that_did_not_run_writes_nothing(hooks: CanvasHooks, doc_id: str) -> None:
    terminal(hooks, "echo x > src/payments/api/a.ts", 0, status="pending_approval")
    assert hooks.store.get(doc_id).require("api").writes == 0


def test_script_that_names_a_removed_path_is_blocked(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.store.update(doc_id, lambda d: apply_op(d, {"op": "remove", "id": "refund"}), "user")
    code = "from pathlib import Path\nPath('src/refund/page.tsx').write_text('x')\n"
    decision = hooks.pre_tool_call(tool_name="execute_code", args={"code": code}, task_id=SID, session_id=SID)
    assert decision is not None and decision["action"] == "block"
    hooks.post_tool_call(
        tool_name="execute_code",
        args={"code": code},
        result=json.dumps({"error": decision["message"]}),
        task_id=SID,
        session_id=SID,
        status="blocked",
        error_message=decision["message"],
    )
    assert hooks.store.get(doc_id).require("refund").blocked == 1
    other = {"code": "print(open('src/payments/api/a.ts').read())"}
    assert hooks.pre_tool_call(tool_name="execute_code", args=other, task_id=SID, session_id=SID) is None


def test_relative_shell_paths_follow_the_cwd_hermes_keeps(hooks: CanvasHooks, doc_id: str) -> None:
    root = hooks.store.get(doc_id).root
    hooks.store.update(doc_id, lambda d: apply_op(d, {"op": "remove", "id": "refund"}), "user")
    # Hermes keeps the shell in `web` after this command and says so in the result.
    result = json.dumps({"output": "", "exit_code": 0, "error": None, "cwd": f"{root}/web"})
    hooks.post_tool_call(
        tool_name="terminal", args={"command": "cd web"}, result=result, task_id=SID, session_id=SID
    )
    args = {"command": "echo x > src/refund/a.ts"}
    assert hooks.pre_tool_call(tool_name="terminal", args=args, task_id=SID, session_id=SID) is None
    terminal(hooks, "echo x > src/payments/api/a.ts", 0)
    assert "web/src/payments/api/a.ts" in [p for b in hooks.store.get(doc_id).boxes for p in b.written]
    # A `workdir` argument wins over the kept cwd.
    args = {"command": "echo x > src/refund/a.ts", "workdir": root}
    assert hooks.pre_tool_call(tool_name="terminal", args=args, task_id=SID, session_id=SID) is not None


def test_relative_shell_writes_wait_for_a_known_cwd(hooks: CanvasHooks, doc_id: str) -> None:
    # A Hermes session can start outside the root, so a relative path is not counted yet ...
    terminal(hooks, "echo x > src/payments/api/a.ts", 0)
    assert hooks.store.get(doc_id).require("api").writes == 0
    # ... but the veto still guesses the root.
    hooks.store.update(doc_id, lambda d: apply_op(d, {"op": "remove", "id": "refund"}), "user")
    args = {"command": "echo x > src/refund/a.ts"}
    assert hooks.pre_tool_call(tool_name="terminal", args=args, task_id=SID, session_id=SID) is not None
    # A failed `cd` teaches nothing.
    terminal(hooks, f"cd {ROOT} && false", 1)
    terminal(hooks, "echo x > src/payments/api/a.ts", 0)
    assert hooks.store.get(doc_id).require("api").writes == 0


def test_a_subagent_cd_makes_the_parent_cwd_unknown(hooks: CanvasHooks, doc_id: str) -> None:
    start_api_subagent(hooks, doc_id)
    terminal(hooks, f"cd {ROOT}", 0)
    result = json.dumps({"output": "", "exit_code": 0, "error": None, "cwd": f"{ROOT}/web"})
    hooks.post_tool_call(
        tool_name="terminal",
        args={"command": "cd web"},
        result=result,
        task_id="sa-0-aaaa1111",
        session_id="c1",
    )
    # Hermes may share one cwd between the parent and its subagents: the parent's is now unknown.
    terminal(hooks, "echo x > src/payments/api/a.ts", 0)
    assert hooks.store.get(doc_id).require("api").writes == 0


def run_shell(hooks: CanvasHooks, command: str, write_to: Path | None = None) -> None:
    """A terminal call as Hermes makes it: pre hook, the command (here: a file write), post hook."""
    args = {"command": command}
    assert hooks.pre_tool_call(tool_name="terminal", args=args, task_id=SID, session_id=SID) is None
    if write_to is not None:
        write_to.parent.mkdir(parents=True, exist_ok=True)
        write_to.write_text("x")
    result = json.dumps({"output": "", "exit_code": 0, "error": None})
    hooks.post_tool_call(tool_name="terminal", args=args, result=result, task_id=SID, session_id=SID)


def test_a_session_in_the_root_counts_relative_writes_without_cd(hooks: CanvasHooks, tmp_path: Path) -> None:
    doc_id = hooks.store.draw(SID, "Shop", str(tmp_path), plan(*SHOP_PLAN)).id
    # The command really wrote the file under the root: it counts.
    run_shell(hooks, "echo x > src/payments/api/a.ts", tmp_path / "src/payments/api/a.ts")
    assert hooks.store.get(doc_id).require("api").written == ["src/payments/api/a.ts"]
    # An old file the command did not change (the shell was elsewhere): it does not count.
    old = tmp_path / "src/payments/api/old.ts"
    old.write_text("x")
    os.utime(old, (1_000_000, 1_000_000))
    run_shell(hooks, "echo x > src/payments/api/old.ts")
    assert hooks.store.get(doc_id).require("api").writes == 1
