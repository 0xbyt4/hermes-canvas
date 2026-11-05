"""Hermes hook callbacks. Box status comes from here, not from the model.

- `pre_tool_call` blocks writes to files of boxes the user removed. It uses only in-memory data.
- `post_tool_call` binds writes to boxes (or records drift), sets check results from `terminal`
  exit codes, and counts blocked writes.
- `subagent_start` / `subagent_stop` map subagents to the box in their goal tag.
- `pre_llm_call` gives the agent the plan and the user's canvas edits it has not seen yet.

A failing callback must never break the user's agent: every callback logs and returns None on
an unexpected error. For `pre_tool_call` that means "allow" (Hermes would otherwise fail closed).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .binding import WRITE_TOOLS, Binding, paths_from_args, paths_from_result, relativize
from .model import MAX_WRITTEN, Box, Doc
from .runtime import CanvasRuntime

logger = logging.getLogger(__name__)

CHECK_OUTPUT_LIMIT = 2000
BLOCK_PREFIX = "Blocked by hermes-canvas:"
# Hermes terminal result for a command that an approval rule stopped (exit_code -1).
NOT_RUN_PREFIX = "BLOCKED:"
CONTEXT_HEADER = "[hermes-canvas]"
MAX_DRIFT_IN_CONTEXT = 5
SUBAGENT_SUCCESS = ("completed", "ok", "success")


class CanvasHooks:
    def __init__(self, runtime: CanvasRuntime) -> None:
        self.runtime = runtime
        self.store = runtime.store

    # -- pre_tool_call ---------------------------------------------------------------------

    def pre_tool_call(
        self, tool_name: str = "", args: Any = None, task_id: str = "", session_id: str = "", **_: Any
    ) -> dict[str, str] | None:
        try:
            return self._pre_tool_call(tool_name, args, task_id, session_id)
        except Exception:  # never fail closed because of a canvas bug
            logger.exception("hermes-canvas: pre_tool_call failed; allowing the call")
            return None

    def _pre_tool_call(
        self, tool_name: str, args: Any, task_id: str, session_id: str
    ) -> dict[str, str] | None:
        if tool_name not in WRITE_TOOLS:
            return None
        doc_id = self.runtime.doc_id_for(task_id, session_id)
        binding = self.runtime.binding(doc_id) if doc_id else None
        if binding is None:
            return None
        for path in paths_from_args(tool_name, args):
            rel = relativize(path, binding.root)
            if rel is None:
                continue
            box_id = binding.removed_box_for_path(rel)
            if box_id is not None:
                return {
                    "action": "block",
                    "message": (
                        f"{BLOCK_PREFIX} the canvas box '{box_id}', which owns {rel}, was removed from "
                        "the plan. "
                        "Do not write this file and do not retry. Continue with the remaining boxes, "
                        "and mention the skipped file in your summary."
                    ),
                }
        return None

    # -- post_tool_call --------------------------------------------------------------------

    def post_tool_call(
        self,
        tool_name: str = "",
        args: Any = None,
        result: Any = None,
        task_id: str = "",
        session_id: str = "",
        status: str = "",
        error_message: str = "",
        **_: Any,
    ) -> None:
        try:
            self._post_tool_call(tool_name, args, result, task_id, session_id, status, error_message)
        except Exception:
            logger.exception("hermes-canvas: post_tool_call failed")

    def _post_tool_call(
        self,
        tool_name: str,
        args: Any,
        result: Any,
        task_id: str,
        session_id: str,
        status: str,
        error_message: str,
    ) -> None:
        if tool_name not in (*WRITE_TOOLS, "terminal"):
            return
        doc_id = self.runtime.doc_id_for(task_id, session_id)
        if doc_id is None:
            return
        if tool_name == "terminal":
            # Hermes reports status "error" for every non-zero exit, so a failing check arrives as
            # "error". Only a plugin block means the command did not run; the result itself tells
            # the other not-run cases apart (see _record_check).
            if status != "blocked":
                self._record_check(doc_id, args, result)
            return
        if status == "blocked":
            if BLOCK_PREFIX in (error_message or ""):
                self._record_block(doc_id, tool_name, args)
            return
        parsed = _parse_json(result)
        if status not in ("", "ok") or (parsed and parsed.get("error")):
            return
        paths = paths_from_result(parsed) or paths_from_args(tool_name, args)
        owner = self.runtime.owner_box(task_id, session_id)
        self._record_writes(doc_id, paths, owner[1] if owner else "", task_id)

    def _record_writes(self, doc_id: str, paths: list[str], owner_box: str, task_id: str) -> None:
        if not paths:
            return

        def mutate(doc: Doc) -> str | None:
            binding = Binding.build(doc)
            touched: list[str] = []
            for path in paths:
                rel = relativize(path, doc.root)
                if rel is None:
                    continue
                box_ids = binding.boxes_for_path(rel)
                if not box_ids:
                    box_ids = [_drift_box(doc, rel).id]
                for box_id in box_ids:
                    box = doc.require(box_id)
                    if box.kind != "drift" and box.status != "doing":
                        box.status = "doing"
                    box.writes += 1
                    if rel in box.written:
                        box.written.remove(rel)
                    box.written.append(rel)
                    del box.written[:-MAX_WRITTEN]
                    if owner_box == box_id and task_id.startswith("sa-"):
                        box.owner = task_id
                    touched.append(f"{box_id}:{rel}")
            return f"write {', '.join(touched)}" if touched else None

        self.store.update(doc_id, mutate, "hooks")

    def _record_check(self, doc_id: str, args: Any, result: Any) -> None:
        command = args.get("command") if isinstance(args, dict) else None
        if not isinstance(command, str) or not command:
            return
        if args.get("background"):
            return  # a background start returns exit 0 before the check has run
        binding = self.runtime.binding(doc_id)
        box_ids = binding.boxes_for_command(command) if binding else []
        if not box_ids:
            return
        parsed = _parse_json(result)
        if parsed is None or parsed.get("status") not in (None, "ok"):
            return  # blocked, pending approval, degraded backend or error: the check did not run
        exit_code = parsed.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            return
        output = "\n".join(str(parsed.get(k) or "") for k in ("output", "error")).strip()
        if exit_code < 0 and output.startswith(NOT_RUN_PREFIX):
            return  # an approval rule stopped the command; the check never ran

        def mutate(doc: Doc) -> str | None:
            changed = []
            for box_id in box_ids:
                box = doc.box(box_id)
                if box is None or box.removed:
                    continue
                box.status = "done" if exit_code == 0 else "failed"
                box.check_exit = exit_code
                box.check_output = output[-CHECK_OUTPUT_LIMIT:]
                changed.append(box_id)
            return f"check {','.join(changed)} exit {exit_code}" if changed else None

        self.store.update(doc_id, mutate, "hooks")

    def _record_block(self, doc_id: str, tool_name: str, args: Any) -> None:
        paths = paths_from_args(tool_name, args)

        def mutate(doc: Doc) -> str | None:
            binding = Binding.build(doc)
            for path in paths:
                rel = relativize(path, doc.root)
                box_id = binding.removed_box_for_path(rel) if rel else None
                if box_id is not None:
                    doc.require(box_id).blocked += 1
                    return f"veto {box_id}:{rel}"
            return None

        self.store.update(doc_id, mutate, "hooks")

    # -- subagents -------------------------------------------------------------------------

    def subagent_start(
        self,
        parent_session_id: str = "",
        parent_subagent_id: str | None = None,
        child_session_id: str | None = None,
        child_subagent_id: str = "",
        child_goal: str = "",
        **_: Any,
    ) -> None:
        try:
            link = self.runtime.register_subagent(
                child_subagent_id or "",
                child_session_id or "",
                parent_session_id or "",
                parent_subagent_id or "",
                child_goal or "",
            )
            if not (link.doc_id and link.box_id):
                return

            def mutate(doc: Doc) -> str | None:
                box = doc.box(link.box_id)
                if box is None or box.removed:
                    return None
                box.owner = link.subagent_id
                if box.status in ("todo", "failed"):
                    box.status = "doing"
                return f"subagent {link.subagent_id} started {box.id}"

            self.store.update(link.doc_id, mutate, "hooks")
        except Exception:
            logger.exception("hermes-canvas: subagent_start failed")

    def subagent_stop(self, child_session_id: str | None = None, child_status: str = "", **_: Any) -> None:
        try:
            link = self.runtime.finish_subagent(child_session_id or "", child_status or "")
            if link is None or not (link.doc_id and link.box_id):
                return

            def mutate(doc: Doc) -> str | None:
                box = doc.box(link.box_id)
                if box is None or box.owner != link.subagent_id or box.status != "doing":
                    return None
                if not box.check and link.finished in SUBAGENT_SUCCESS:
                    # No check to wait for: the child's own success is the only signal.
                    box.status = "done"
                    return f"subagent {link.subagent_id} completed {box.id} (no check)"
                # The child ended before its check passed: offer the box again via canvas_next.
                box.status = "todo"
                box.owner = ""
                return f"subagent {link.subagent_id} ended ({link.finished}) before {box.id} passed its check"

            self.store.update(link.doc_id, mutate, "hooks")
        except Exception:
            logger.exception("hermes-canvas: subagent_stop failed")

    # -- pre_llm_call ----------------------------------------------------------------------

    def pre_llm_call(self, session_id: str = "", task_id: str = "", **_: Any) -> dict[str, str] | None:
        try:
            return self._pre_llm_call(session_id, task_id)
        except Exception:
            logger.exception("hermes-canvas: pre_llm_call failed")
            return None

    def _pre_llm_call(self, session_id: str, task_id: str) -> dict[str, str] | None:
        doc_id = self.runtime.doc_id_for(task_id, session_id)
        doc = self.store.get(doc_id) if doc_id else None
        if doc is None:
            return None
        if self.runtime.link_for(task_id, session_id) is not None:
            # Subagents get their box (or the plan), never the user's pending edits: those belong
            # to the main agent, which owns the plan.
            owner = self.runtime.owner_box(task_id, session_id)
            box = doc.box(owner[1]) if owner else None
            return {"context": subagent_context(doc, box) if box is not None else plan_context(doc)}
        edits = self.store.take_pending_edits(doc.id)
        lines = [plan_context(doc)]
        if edits:
            lines.append(edits_context([e.text for e in edits]))
        return {"context": "\n\n".join(lines)}


def plan_context(doc: Doc) -> str:
    rows = [
        f"{CONTEXT_HEADER} Canvas {doc.id} (revision {doc.revision}) shows your plan to the user. "
        "Box status comes from your real file writes and check commands. Current boxes:"
    ]
    drift = [b for b in doc.active_boxes() if b.kind == "drift"]
    for box in doc.active_boxes():
        if box.kind != "drift":
            rows.append("- " + box_summary(box))
    if drift:
        shown = ", ".join(b.files[0] for b in drift[:MAX_DRIFT_IN_CONTEXT] if b.files)
        more = f" and {len(drift) - MAX_DRIFT_IN_CONTEXT} more" if len(drift) > MAX_DRIFT_IN_CONTEXT else ""
        rows.append(f"Writes outside every box (drift): {shown}{more}")
    removed = [b.id for b in doc.boxes if b.removed]
    if removed:
        rows.append(f"Removed from the plan (do not build): {', '.join(removed)}")
    return "\n".join(rows)


def edits_context(texts: list[str]) -> str:
    rows = [
        f"{CONTEXT_HEADER} The user changed the plan on the canvas next to the chat. These are the "
        "user's own instructions, as authoritative as a chat message from the user. Apply them:"
    ]
    rows.extend(f"- {t}" for t in texts)
    return "\n".join(rows)


def subagent_context(doc: Doc, box: Box) -> str:
    parts = [
        f"{CONTEXT_HEADER} You build canvas box '{box.id}' ({box.label}) of canvas {doc.id}.",
        f"Project root: {doc.root}.",
    ]
    if box.files:
        parts.append(f"Write only files that match: {', '.join(box.files)}. Other writes show as drift.")
    if box.check:
        parts.append(f"Run this check before you finish: {box.check}")
    if box.note:
        parts.append(f"User note on this box: {box.note}")
    return " ".join(parts)


def box_summary(box: Box) -> str:
    text = f"{box.id} [{box.status}] {box.label}"
    if box.kind == "question":
        text += f" (question; options: {', '.join(box.options)}"
        text += f"; user answered: {box.answer})" if box.answer else "; not answered yet)"
        return text
    details = []
    if box.files:
        details.append(f"files: {', '.join(box.files)}")
    if box.check:
        details.append(f"check: {box.check}")
    if box.after:
        details.append(f"after: {', '.join(box.after)}")
    if box.check_exit not in (None, 0):
        details.append(f"last check exit {box.check_exit}")
    return text + (f" ({'; '.join(details)})" if details else "")


def _drift_box(doc: Doc, rel: str) -> Box:
    for box in doc.boxes:
        if box.kind == "drift" and box.files == [rel]:
            return box
    used = {b.id for b in doc.boxes}
    n = 1
    while f"drift-{n}" in used:
        n += 1
    box = Box(id=f"drift-{n}", label=rel, kind="drift", files=[rel], status="doing")
    doc.boxes.append(box)
    return box


def _parse_json(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
