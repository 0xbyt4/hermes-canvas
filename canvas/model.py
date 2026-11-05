"""Canvas document model and the edit operations that change it.

A canvas document belongs to one chat session. It holds boxes. Each box can be bound to file
globs and a check command, so hooks can set its status from real tool calls.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any

BOX_KINDS = ("component", "question", "note", "drift")
STATUSES = ("todo", "doing", "done", "failed")
MAX_TEXT = 4000
MAX_WRITTEN = 20
MAX_LIST = 50
MAX_COORD = 100_000  # board units; the pane draws boxes about 200 wide
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class CanvasError(ValueError):
    """An edit or a plan is not valid. The message is safe to show to the model."""


def _text(value: Any, name: str, *, limit: int = MAX_TEXT, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise CanvasError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise CanvasError(f"{name} is required")
    if len(value) > limit:
        raise CanvasError(f"{name} is longer than {limit} characters")
    return value


def _str_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise CanvasError(f"{name} must be a list of strings")
    items = [v.strip() for v in value if v.strip()]
    if len(items) > MAX_LIST:
        raise CanvasError(f"{name} has more than {MAX_LIST} items")
    return items


def _box_id(value: Any) -> str:
    box_id = _text(value, "id", limit=64, required=True)
    if not ID_RE.match(box_id):
        raise CanvasError(f"box id {box_id!r} must use letters, digits, '_', '.' or '-'")
    return box_id


@dataclass
class Box:
    id: str
    label: str
    kind: str = "component"
    files: list[str] = field(default_factory=list)
    check: str = ""
    after: list[str] = field(default_factory=list)
    status: str = "todo"
    removed: bool = False
    owner: str = ""
    writes: int = 0
    written: list[str] = field(default_factory=list)
    blocked: int = 0
    check_exit: int | None = None
    check_output: str = ""
    options: list[str] = field(default_factory=list)
    answer: str = ""
    note: str = ""
    x: float | None = None
    y: float | None = None

    @classmethod
    def from_plan(cls, raw: Any) -> Box:
        """Build a box from model input. Unknown keys (and x/y) are ignored."""
        if not isinstance(raw, dict):
            raise CanvasError("each box must be an object")
        kind = raw.get("kind") or ("question" if raw.get("options") else "component")
        if kind not in BOX_KINDS:
            raise CanvasError(f"box kind must be one of {', '.join(BOX_KINDS)}")
        box = cls(
            id=_box_id(raw.get("id")),
            label=_text(raw.get("label"), "label", limit=200, required=True),
            kind=kind,
            files=_str_list(raw.get("files"), "files"),
            check=_text(raw.get("check"), "check", limit=500),
            after=_str_list(raw.get("after"), "after"),
            options=_str_list(raw.get("options"), "options"),
        )
        # Position is the user's: the pane lays boxes out to fit itself, and only a `move` op on
        # the canvas pins a box. Model coordinates are ignored.
        return box

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Box:
        """Load a stored box. Stored data is trusted, but keep only known fields."""
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Doc:
    id: str
    session_id: str
    title: str
    root: str
    boxes: list[Box] = field(default_factory=list)
    revision: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    def box(self, box_id: str) -> Box | None:
        for box in self.boxes:
            if box.id == box_id:
                return box
        return None

    def require(self, box_id: str) -> Box:
        box = self.box(box_id)
        if box is None:
            raise CanvasError(f"no box with id {box_id!r}")
        return box

    def active_boxes(self) -> list[Box]:
        return [b for b in self.boxes if not b.removed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "title": self.title,
            "root": self.root,
            "boxes": [b.to_dict() for b in self.boxes],
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Doc:
        return cls(
            id=data["id"],
            session_id=data["session_id"],
            title=data.get("title", ""),
            root=data.get("root", ""),
            boxes=[Box.from_dict(b) for b in data.get("boxes", [])],
            revision=int(data.get("revision", 0)),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
        )


def validate_plan(boxes: list[Box]) -> None:
    """Reject duplicate ids, unknown dependencies and dependency cycles."""
    ids = [b.id for b in boxes]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise CanvasError(f"duplicate box ids: {', '.join(duplicates)}")
    known = set(ids)
    for box in boxes:
        unknown = [a for a in box.after if a not in known]
        if unknown:
            raise CanvasError(f"box {box.id!r} waits for unknown boxes: {', '.join(unknown)}")
    _check_cycles(boxes)


def _check_cycles(boxes: list[Box]) -> None:
    after = {b.id: list(b.after) for b in boxes}
    state: dict[str, int] = {}

    def visit(node: str, path: list[str]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            cycle = [*path[path.index(node) :], node]
            raise CanvasError(f"dependency cycle: {' -> '.join(cycle)}")
        state[node] = 1
        for dep in after.get(node, []):
            visit(dep, [*path, node])
        state[node] = 2

    for node in after:
        visit(node, [])


def ready_boxes(doc: Doc) -> list[Box]:
    """Component boxes that can start now: not removed, not done, all `after` boxes done.

    Removed boxes do not block the boxes that wait for them. Order follows the document.
    """
    done = {b.id for b in doc.boxes if b.status == "done" or b.removed}
    return [
        b
        for b in doc.active_boxes()
        if b.kind == "component" and b.status in ("todo", "failed") and all(a in done for a in b.after)
    ]


def apply_op(doc: Doc, op: Any) -> str:
    """Apply one edit operation to `doc` in place. Returns a short description of the change."""
    if not isinstance(op, dict):
        raise CanvasError("each op must be an object")
    name = op.get("op")
    handler = _OPS.get(name) if isinstance(name, str) else None
    if handler is None:
        raise CanvasError(f"unknown op {name!r}; use one of {', '.join(sorted(_OPS))}")
    return handler(doc, op)


def _op_add(doc: Doc, op: dict[str, Any]) -> str:
    box = Box.from_plan(op.get("box"))
    if doc.box(box.id) is not None:
        raise CanvasError(f"box {box.id!r} already exists")
    validate_plan([*doc.boxes, box])
    doc.boxes.append(box)
    return f"added {box.id}"


def _op_remove(doc: Doc, op: dict[str, Any]) -> str:
    box = doc.require(_box_id(op.get("id")))
    box.removed = True
    return f"removed {box.id}"


def _op_restore(doc: Doc, op: dict[str, Any]) -> str:
    box = doc.require(_box_id(op.get("id")))
    box.removed = False
    return f"restored {box.id}"


def _op_rename(doc: Doc, op: dict[str, Any]) -> str:
    box = doc.require(_box_id(op.get("id")))
    box.label = _text(op.get("label"), "label", limit=200, required=True)
    return f"renamed {box.id}"


def _op_set_files(doc: Doc, op: dict[str, Any]) -> str:
    box = doc.require(_box_id(op.get("id")))
    box.files = _str_list(op.get("files"), "files")
    return f"set files of {box.id}"


def _op_set_check(doc: Doc, op: dict[str, Any]) -> str:
    box = doc.require(_box_id(op.get("id")))
    box.check = _text(op.get("check"), "check", limit=500)
    return f"set check of {box.id}"


def _op_set_after(doc: Doc, op: dict[str, Any]) -> str:
    box = doc.require(_box_id(op.get("id")))
    previous = box.after
    box.after = _str_list(op.get("after"), "after")
    try:
        validate_plan(doc.boxes)
    except CanvasError:
        box.after = previous
        raise
    return f"set dependencies of {box.id}"


def _op_set_status(doc: Doc, op: dict[str, Any]) -> str:
    box = doc.require(_box_id(op.get("id")))
    status = op.get("status")
    if status not in STATUSES:
        raise CanvasError(f"status must be one of {', '.join(STATUSES)}")
    box.status = status
    return f"set status of {box.id} to {status}"


def _op_answer(doc: Doc, op: dict[str, Any]) -> str:
    box = doc.require(_box_id(op.get("id")))
    if box.kind != "question":
        raise CanvasError(f"box {box.id!r} is not a question")
    answer = _text(op.get("answer"), "answer", limit=500, required=True)
    box.answer = answer
    box.status = "done"
    return f"answered {box.id}: {answer}"


def _op_move(doc: Doc, op: dict[str, Any]) -> str:
    box = doc.require(_box_id(op.get("id")))
    for axis in ("x", "y"):
        value = op.get(axis)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise CanvasError(f"{axis} must be a number")
        # JSON parsing turns 1e999 into inf, which the store keeps but no JSON response can carry.
        if not math.isfinite(value) or abs(value) > MAX_COORD:
            raise CanvasError(f"{axis} must be a finite number within ±{MAX_COORD:g}")
        setattr(box, axis, float(value))
    return f"moved {box.id}"


_OPS = {
    "add": _op_add,
    "remove": _op_remove,
    "restore": _op_restore,
    "rename": _op_rename,
    "set_files": _op_set_files,
    "set_check": _op_set_check,
    "set_after": _op_set_after,
    "set_status": _op_set_status,
    "answer": _op_answer,
    "move": _op_move,
}
OP_NAMES = tuple(sorted(_OPS))
