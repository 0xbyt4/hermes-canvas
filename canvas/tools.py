"""Model-facing tools: canvas_draw, canvas_update, canvas_get and canvas_next.

Handlers take the tool arguments plus the `task_id` / `session_id` kwargs Hermes injects, and
return a JSON string. Validation errors come back as `{"error": ...}` so the model can fix them.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from .binding import goal_tag, normalize_globs
from .hooks import box_summary, note_tag
from .model import OP_NAMES, Box, CanvasError, Doc, apply_op, ready_boxes
from .runtime import CanvasRuntime

TOOLSET = "hermes_canvas"

_BOX_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Short stable id, e.g. 'api'. Letters, digits, _ . -"},
        "label": {"type": "string", "description": "What this box delivers, in a few words."},
        "files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Globs relative to root for the files this box writes, e.g. 'src/payments/**'.",
        },
        "check": {
            "type": "string",
            "description": "Shell command that proves the box works, e.g. 'npm test -- payments'. "
            "Run exactly this command with the terminal tool; its exit code sets the box status.",
        },
        "after": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ids of boxes to finish first.",
        },
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Only for a decision the user must make: the choices. The box becomes a question.",
        },
    },
    "required": ["id", "label"],
}

DRAW_SCHEMA: dict[str, Any] = {
    "name": "canvas_draw",
    "description": (
        "Draw your plan for a multi-step coding task on the canvas next to the chat, before you write code. "
        "The user can remove, add or annotate boxes there. Bind every box to the files it writes and to a "
        "check command: hooks then set the box status from your real writes and check exit codes. Calling "
        "it again replaces the plan and keeps the state of boxes that keep their id. After drawing, put the "
        "returned `card` line on its own line in your reply so the user sees the canvas."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short plan title."},
            "root": {
                "type": "string",
                "description": "Absolute path of the project root. Globs are relative to it.",
            },
            "boxes": {"type": "array", "items": _BOX_SCHEMA, "description": "Plan boxes in build order."},
        },
        "required": ["title", "boxes"],
    },
}

UPDATE_SCHEMA: dict[str, Any] = {
    "name": "canvas_update",
    "description": (
        "Change the canvas plan with a list of ops, applied together (all or nothing). Ops: "
        "add {box}, remove {id}, restore {id}, rename {id, label}, set_files {id, files}, "
        "set_check {id, check}, set_after {id, after}, "
        "set_status {id, status} (only when no write or check can show it), "
        "answer {id, answer}, move {id, x, y}, "
        "adopt {id, label?} (make a drift box a planned box that owns its file)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ops": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"op": {"type": "string", "enum": list(OP_NAMES)}},
                    "required": ["op"],
                },
            }
        },
        "required": ["ops"],
    },
}

GET_SCHEMA: dict[str, Any] = {
    "name": "canvas_get",
    "description": "Read the current canvas: every box with its status, files, check result, and the canvas "
    "edits from the user that you have not seen yet. Treat those edits as instructions from the user.",
    "parameters": {"type": "object", "properties": {}},
}

NEXT_SCHEMA: dict[str, Any] = {
    "name": "canvas_next",
    "description": "List the boxes that can start now (all their `after` boxes are done), each with a "
    "ready delegate_task goal. Keep the [canvas:...] tag at the start of the goal: it links the subagent "
    "to its box.",
    "parameters": {"type": "object", "properties": {}},
}


class CanvasTools:
    def __init__(self, runtime: CanvasRuntime, default_root: Callable[[], str] | None = None) -> None:
        self.runtime = runtime
        self.store = runtime.store
        self._default_root = default_root or _default_root

    def specs(self) -> list[tuple[dict[str, Any], Callable[..., str]]]:
        return [
            (DRAW_SCHEMA, self.canvas_draw),
            (UPDATE_SCHEMA, self.canvas_update),
            (GET_SCHEMA, self.canvas_get),
            (NEXT_SCHEMA, self.canvas_next),
        ]

    def canvas_draw(self, args: dict[str, Any], task_id: str = "", session_id: str = "") -> str:
        def run() -> dict[str, Any]:
            title = args.get("title")
            if not isinstance(title, str) or not title.strip():
                raise CanvasError("title is required")
            raw_boxes = args.get("boxes")
            if not isinstance(raw_boxes, list) or not raw_boxes:
                raise CanvasError("boxes must be a non-empty list")
            root = args.get("root") or self._default_root()
            if not isinstance(root, str) or not os.path.isabs(os.path.expanduser(root)):
                raise CanvasError("root must be an absolute path")
            root = os.path.normpath(os.path.expanduser(root))
            boxes = [Box.from_plan(b) for b in raw_boxes]
            normalize_globs(boxes, root)
            owner = self.runtime.root_session(task_id, session_id)
            if not owner:
                raise CanvasError("no session to attach the canvas to")
            doc = self.store.draw(owner, title.strip()[:200], root, boxes)
            return {
                "canvas_id": doc.id,
                "revision": doc.revision,
                "card": card(doc.id),
                # The turn-start context has no key yet in the turn that draws the canvas.
                "note_tag": note_tag(self.runtime.note_key(doc.id)),
                "boxes": [box_summary(b) for b in doc.active_boxes()],
                "ready": [b.id for b in ready_boxes(doc)],
            }

        return _respond(run)

    def canvas_update(self, args: dict[str, Any], task_id: str = "", session_id: str = "") -> str:
        def run() -> dict[str, Any]:
            ops = args.get("ops")
            if not isinstance(ops, list) or not ops:
                raise CanvasError("ops must be a non-empty list")
            doc_id = self._doc_id(task_id, session_id)

            def mutate(doc: Doc) -> str:
                reason = "; ".join(apply_op(doc, op) for op in ops)
                normalize_globs(doc.boxes, doc.root)
                return reason

            doc = self.store.update(doc_id, mutate, "agent")
            current = doc or self.store.get(doc_id)
            return {
                "canvas_id": doc_id,
                "revision": current.revision if current else None,
                "applied": len(ops),
            }

        return _respond(run)

    def canvas_get(self, args: dict[str, Any], task_id: str = "", session_id: str = "") -> str:
        def run() -> dict[str, Any]:
            doc = self._doc(task_id, session_id)
            # The main agent owns the plan, so reading the canvas delivers the user's edits to it.
            # A subagent sees them too, but they stay pending for the main agent.
            if self.runtime.link_for(task_id, session_id) is None:
                edits = self.store.take_pending_edits(doc.id)
            else:
                edits = self.store.pending_edits(doc.id)
            return {
                "canvas_id": doc.id,
                "title": doc.title,
                "root": doc.root,
                "revision": doc.revision,
                "note_tag": note_tag(self.runtime.note_key(doc.id)),
                "boxes": [_box_view(b) for b in doc.boxes],
                "user_edits": [e.text for e in edits],
            }

        return _respond(run)

    def canvas_next(self, args: dict[str, Any], task_id: str = "", session_id: str = "") -> str:
        def run() -> dict[str, Any]:
            doc = self._doc(task_id, session_id)
            ready = ready_boxes(doc)
            return {
                "canvas_id": doc.id,
                "ready": [{"id": b.id, "label": b.label, "goal": delegate_goal(doc, b)} for b in ready],
                "open_questions": [b.id for b in doc.active_boxes() if b.kind == "question" and not b.answer],
            }

        return _respond(run)

    def _doc_id(self, task_id: str, session_id: str) -> str:
        doc_id = self.runtime.doc_id_for(task_id, session_id)
        if doc_id is None:
            raise CanvasError("this chat has no canvas yet; call canvas_draw first")
        return doc_id

    def _doc(self, task_id: str, session_id: str) -> Doc:
        doc = self.store.get(self._doc_id(task_id, session_id))
        if doc is None:
            raise CanvasError("the canvas of this chat is missing; call canvas_draw again")
        return doc


def card(doc_id: str) -> str:
    return f'::hermes-canvas{{id="{doc_id}"}}'


def delegate_goal(doc: Doc, box: Box) -> str:
    parts = [f"{goal_tag(doc.id, box.id)} {box.label}.", f"Project root: {doc.root}."]
    if box.files:
        parts.append(f"Write only: {', '.join(box.files)}.")
    if box.check:
        parts.append(f"Done when this passes: {box.check}")
    if box.note:
        parts.append(f"User note: {box.note}")
    return " ".join(parts)


def _box_view(box: Box) -> dict[str, Any]:
    view = {
        "id": box.id,
        "label": box.label,
        "kind": box.kind,
        "status": box.status,
        "files": box.files,
        "check": box.check,
        "after": box.after,
        "writes": box.writes,
    }
    if box.removed:
        view["removed"] = True
    if box.check_exit is not None:
        view["check_exit"] = box.check_exit
        view["check_output_tail"] = box.check_output[-600:]
    if box.kind == "question":
        view["options"] = box.options
        view["answer"] = box.answer
    if box.note:
        view["note"] = box.note
    if box.owner:
        view["owner"] = box.owner
    return view


def _respond(run: Callable[[], dict[str, Any]]) -> str:
    try:
        return json.dumps(run(), ensure_ascii=False)
    except CanvasError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def _default_root() -> str:
    env_root = os.environ.get("TERMINAL_CWD", "")
    if env_root and os.path.isabs(os.path.expanduser(env_root)):
        return env_root
    return os.getcwd()
