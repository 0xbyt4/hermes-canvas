"""What the desktop pane can read and change, independent of the web framework.

`dashboard/plugin_api.py` maps these methods to HTTP routes. User edits go through the same ops as
`canvas_update`, are saved with author "user", and are queued as pending edits so the agent reads
them as the user's own instructions on its next turn (see `hooks.edits_context`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .model import Box, CanvasError, Doc, apply_op, ready_boxes
from .runtime import CanvasRuntime
from .store import EditSpec

logger = logging.getLogger(__name__)

# Ops the pane may send. Plan-shaping ops (add, set_files, set_check, set_after) stay with the agent.
USER_OPS = ("remove", "restore", "rename", "move", "answer")
MAX_OPS = 20
EVENT_CHANGED = "changed"

Emit = Callable[[str, dict[str, Any]], None]


class CanvasApi:
    def __init__(self, runtime: CanvasRuntime) -> None:
        self.runtime = runtime
        self.store = runtime.store

    def canvas(self, session_id: str = "", canvas_id: str = "", since: int | None = None) -> dict[str, Any]:
        """The canvas of a chat session (or a canvas by id), or `{"canvas": None}` when none exists.

        With `since`, an unchanged revision returns only `{"unchanged": True, "revision": n}`, so
        polling stays cheap.
        """
        doc_id = canvas_id or (self.runtime.doc_id_for(session_id, session_id) if session_id else None)
        if not doc_id:
            return {"canvas": None}
        if since is not None:
            current = self.store.current_revision(doc_id)
            if current is not None and current == since:
                # The agent takes edits without saving a revision, so the count can change alone.
                pending = len(self.store.pending_edits(doc_id))
                return {"unchanged": True, "revision": current, "pending_edits": pending}
        doc = self.store.get(doc_id)
        if doc is None:
            return {"canvas": None}
        return {"canvas": doc_view(doc), "pending_edits": len(self.store.pending_edits(doc.id))}

    def edit(self, canvas_id: str, ops: Any) -> dict[str, Any]:
        """Apply the user's ops all-or-nothing and queue a note for the agent for each one it must know."""
        if not isinstance(ops, list) or not ops:
            raise CanvasError("ops must be a non-empty list")
        if len(ops) > MAX_OPS:
            raise CanvasError(f"at most {MAX_OPS} ops per edit")
        for op in ops:
            name = op.get("op") if isinstance(op, dict) else None
            if name not in USER_OPS:
                raise CanvasError(
                    f"op {name!r} is not available on the canvas; use one of {', '.join(USER_OPS)}"
                )
        notes: list[EditSpec] = []

        def mutate(doc: Doc) -> str:
            notes.clear()  # the store can run this again on a fresh document
            reasons = []
            for op in ops:
                before = _box_copy(doc, op.get("id"))
                reasons.append(apply_op(doc, op))
                note = edit_note(op, before, doc.box(str(op.get("id"))))
                if note is not None:
                    notes.append((op["op"], note, str(op.get("id"))))
            return "; ".join(reasons)

        # The notes are saved with the revision: the agent never sees the change without them.
        doc = self.store.update(canvas_id, mutate, "user", edits=notes)
        current = doc or self.store.get(canvas_id)
        if current is None:
            raise CanvasError(f"no canvas with id {canvas_id!r}")
        return {"canvas": doc_view(current), "pending_edits": len(self.store.pending_edits(canvas_id))}

    def history(self, canvas_id: str) -> dict[str, Any]:
        if self.store.get(canvas_id) is None:
            raise CanvasError(f"no canvas with id {canvas_id!r}")
        return {
            "canvas_id": canvas_id,
            "revisions": [
                {"revision": h.revision, "author": h.author, "reason": h.reason, "at": h.created_at}
                for h in self.store.history(canvas_id)
            ],
        }

    def revision(self, canvas_id: str, revision: int) -> dict[str, Any]:
        doc = self.store.revision(canvas_id, revision)
        if doc is None:
            raise CanvasError(f"canvas {canvas_id!r} has no revision {revision}")
        return {"canvas": doc_view(doc)}


def change_event(doc: Doc, author: str, reason: str) -> dict[str, Any]:
    """Payload of `plugin.hermes-canvas.changed`. Global plugin frames have an empty session id,
    so the session goes in the payload."""
    return {
        "session_id": doc.session_id,
        "canvas_id": doc.id,
        "revision": doc.revision,
        "author": author,
        "reason": reason[:200],
    }


def broadcaster(emit: Emit) -> Callable[[Doc, str, str], None]:
    """A store listener that announces every saved revision. Failures never reach the store."""

    def listener(doc: Doc, author: str, reason: str) -> None:
        try:
            emit(EVENT_CHANGED, change_event(doc, author, reason))
        except Exception:
            logger.exception("hermes-canvas: change broadcast failed")

    return listener


def doc_view(doc: Doc) -> dict[str, Any]:
    data = doc.to_dict()
    data["ready"] = [b.id for b in ready_boxes(doc)]
    return data


def edit_note(op: dict[str, Any], before: Box | None, after: Box | None) -> str | None:
    """The sentence the agent reads for one user edit, or None when the agent need not know.

    A move never needs a note, and neither does an op that changed nothing (for example a second
    remove sent by a stale pane).
    """
    name = op.get("op")
    if after is None or before is None or name == "move":
        return None
    label = f"'{after.id}' ({after.label})"
    if name == "remove" and not before.removed:
        return f"Removed box {label} from the plan. Do not build it, and stop work on it now."
    if name == "restore" and before.removed:
        if after.status == "done":
            return f"Put box {label} back in the plan. It is already done: keep it, do not rebuild it."
        return f"Put box {label} back in the plan. Build it."
    if name == "rename" and before.label != after.label:
        return (
            f"Renamed box '{after.id}' from '{before.label}' to '{after.label}'. Use the new name and intent."
        )
    if name == "answer" and before.answer != after.answer:
        return f"Answered the question {label}: {after.answer}. Build with this choice."
    return None


def _box_copy(doc: Doc, box_id: Any) -> Box | None:
    box = doc.box(str(box_id)) if box_id is not None else None
    return Box.from_dict(box.to_dict()) if box is not None else None
