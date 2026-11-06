"""What the desktop pane can read and change, independent of the web framework.

`dashboard/plugin_api.py` maps these methods to HTTP routes. User edits go through the same ops as
`canvas_update`, are saved with author "user", and are queued as pending edits so the agent reads
them as the user's own instructions on its next turn (see `hooks.edits_context`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from .diffs import box_diffs
from .model import Box, CanvasError, Doc, apply_op, note_edit_text, one_line, quote_path, ready_boxes
from .runtime import CanvasRuntime
from .store import EditSpec

logger = logging.getLogger(__name__)

# Ops the pane may send. The user can add a box by name and add or drop one dependency, but files,
# checks and the whole dependency list (set_files, set_check, set_after) stay with the agent.
USER_OPS = (
    "add",
    "remove",
    "restore",
    "rename",
    "move",
    "answer",
    "adopt",
    "note",
    "link",
    "unlink",
    "revert",
)
USER_ADD_KEYS = ("id", "label", "after")
MAX_OPS = 20
MAX_RECENT_EDITS = 20
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
        ops = [_user_op(op) for op in ops]
        notes: list[EditSpec] = []

        def mutate(doc: Doc) -> str:
            notes.clear()  # the store can run this again on a fresh document
            reasons = []
            for op in ops:
                box_id = _target_id(op)
                before = _box_copy(doc, box_id)
                reasons.append(apply_op(doc, op))
                waiting = [b.id for b in doc.active_boxes() if box_id in b.after]
                note = edit_note(op, before, doc.box(box_id), waiting)
                if note is not None:
                    notes.append((op["op"], note, box_id))
            return "; ".join(reasons)

        # The notes are saved with the revision: the agent never sees the change without them.
        doc = self.store.update(canvas_id, mutate, "user", edits=notes)
        current = doc or self.store.get(canvas_id)
        if current is None:
            raise CanvasError(f"no canvas with id {canvas_id!r}")
        return {"canvas": doc_view(current), "pending_edits": len(self.store.pending_edits(canvas_id))}

    def edits(self, canvas_id: str, limit: int = 8) -> dict[str, Any]:
        """The user's latest edits with the exact text the agent reads, and whether it got them."""
        if self.store.get(canvas_id) is None:
            raise CanvasError(f"no canvas with id {canvas_id!r}")
        recent = self.store.recent_edits(canvas_id, max(1, min(limit, MAX_RECENT_EDITS)))
        return {
            "canvas_id": canvas_id,
            "edits": [
                {
                    "id": e.id,
                    "kind": e.kind,
                    "box_id": e.box_id,
                    "text": e.text,
                    "at": e.created_at,
                    "delivered_at": e.delivered_at,
                }
                for e in recent
            ],
            "pending_edits": len(self.store.pending_edits(canvas_id)),
        }

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

    def diff(self, canvas_id: str, box_id: str) -> dict[str, Any]:
        """Git diffs of the files the box wrote, for the pane's inspector."""
        doc = self.store.get(canvas_id)
        if doc is None:
            raise CanvasError(f"no canvas with id {canvas_id!r}")
        box = doc.require(box_id)
        return {"canvas_id": canvas_id, "box_id": box.id, "files": box_diffs(doc.root, list(box.written))}

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


def edit_note(
    op: dict[str, Any], before: Box | None, after: Box | None, waiting: Sequence[str] = ()
) -> str | None:
    """The sentence the agent reads for one user edit, or None when the agent need not know.

    A move never needs a note, and neither does an op that changed nothing (for example a second
    remove sent by a stale pane).
    """
    name = op.get("op")
    if after is None or name == "move":
        return None
    # Labels are one line when they are set; stored data from older versions may not be.
    label = f"'{after.id}' ({one_line(after.label)})"
    if name == "add":
        if before is not None:
            return None
        after_text = f", after {', '.join(repr(a) for a in after.after)}" if after.after else ""
        return (
            f"Added box {label} to the plan{after_text}. Give it files and a check with canvas_update, "
            "then build it like the other boxes."
        )
    if before is None:
        return None
    if name == "remove" and not before.removed:
        text = f"Removed box {label} from the plan. Do not build it, and stop work on it now."
        if waiting:
            # A removed box no longer blocks the boxes after it; say so, or the agent waits for it.
            names = ", ".join(f"'{w}'" for w in waiting)
            if len(waiting) == 1:
                text += f" Box {names} no longer waits for it: build it once its other dependencies are done."
            else:
                text += (
                    f" Boxes {names} no longer wait for it: build them once their other dependencies "
                    "are done."
                )
        return text
    if name == "restore" and before.removed:
        if after.kind == "drift":
            files = ", ".join(quote_path(f) for f in after.files)
            return (
                f"Put drift box '{after.id}' back on the canvas: the user wants to see your change to "
                f"{files}. Do not undo it now. If you already undid it, say so."
            )
        if after.status == "done":
            text = f"Put box {label} back in the plan. It is already done: keep it, do not rebuild it."
        else:
            text = f"Put box {label} back in the plan. Build it."
        if waiting:
            # The removal note said these boxes no longer wait for it; take that back, or the agent
            # drops the dependency from the plan.
            names = ", ".join(f"'{w}'" for w in waiting)
            if len(waiting) == 1:
                text += f" Box {names} waits for it again."
            else:
                text += f" Boxes {names} wait for it again."
        return text
    if name == "rename" and before.label != after.label:
        return (
            f"Renamed box '{after.id}' from '{one_line(before.label)}' to '{one_line(after.label)}'. "
            "Use the new name and intent."
        )
    if name == "adopt" and before.kind == "drift":
        files = ", ".join(quote_path(f) for f in after.files)
        return (
            f"Added {files}, which you wrote outside the plan, to the plan as box '{after.id}' "
            f"({one_line(after.label)}). Keep it as part of the work."
        )
    if name == "note" and before.note != after.note:
        return note_edit_text(label, after.note)
    # Notes come from what changed, not from the op text: ids in ops are trimmed by the model.
    added = [a for a in after.after if a not in before.after]
    dropped = [a for a in before.after if a not in after.after]
    if name == "link" and added:
        dep = added[0]
        return f"Made box {label} wait for box '{dep}'. Finish '{dep}' before you build {label}."
    if name == "unlink" and dropped:
        dep = dropped[0]
        return f"Box {label} no longer waits for box '{dep}'. It can start without it."
    if name == "revert" and not before.revert_asked:
        files = ", ".join(quote_path(f) for f in after.files)
        return (
            f"Undo your own change to {files} (drift box '{after.id}'). Undo only what you wrote: the "
            "user may have uncommitted work in it. If `git diff` shows only your change, "
            "`git checkout -- <file>` restores it; if you created the file, delete it. If the file "
            "has changes you did not make, undo your edit by hand, or ask the user. "
            f"Then remove box '{after.id}' with canvas_update. If a check fails without this change, "
            "fix it another way, or stop and ask the user."
        )
    if name == "answer" and before.answer != after.answer:
        return f"Answered the question {label}: {after.answer}. Build with this choice."
    return None


def _user_op(op: Any) -> dict[str, Any]:
    """Check that the pane may send this op. A user's new box gets only an id, a label and
    dependencies: its files and check come from the agent."""
    name = op.get("op") if isinstance(op, dict) else None
    if name not in USER_OPS:
        raise CanvasError(f"op {name!r} is not available on the canvas; use one of {', '.join(USER_OPS)}")
    if name != "add":
        return op
    raw = op.get("box")
    if not isinstance(raw, dict):
        raise CanvasError("add needs a box object")
    return {"op": "add", "box": {k: raw[k] for k in USER_ADD_KEYS if k in raw} | {"kind": "component"}}


def _target_id(op: dict[str, Any]) -> str:
    box = op.get("box")
    value = box.get("id") if op.get("op") == "add" and isinstance(box, dict) else op.get("id")
    return str(value) if value is not None else ""


def _box_copy(doc: Doc, box_id: Any) -> Box | None:
    box = doc.box(str(box_id)) if box_id is not None else None
    return Box.from_dict(box.to_dict()) if box is not None else None
