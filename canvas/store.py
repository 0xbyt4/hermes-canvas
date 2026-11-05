"""SQLite store for canvas documents, their revisions and the user's pending canvas edits.

The connection comes from the caller: `plugins.plugin_storage.plugin_db()` in Hermes, an in-memory
database in tests. Hooks run on helper threads, so every method takes the store lock.

More than one process can open the same database (the CLI, the Desktop backend). Every write is
one transaction, and a document update only succeeds when the stored revision is still the one it
read, so a concurrent writer never loses a change: the losing side reloads and applies again.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .model import Box, CanvasError, Doc, validate_plan

EditSpec = tuple[str, str, str]  # kind, text, box_id

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    body TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS docs_session ON docs (session_id, updated_at);
CREATE TABLE IF NOT EXISTS revisions (
    doc_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    author TEXT NOT NULL,
    reason TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (doc_id, revision)
);
CREATE TABLE IF NOT EXISTS edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    box_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    delivered_at REAL
);
CREATE INDEX IF NOT EXISTS edits_pending ON edits (doc_id, delivered_at);
"""

AUTHORS = ("agent", "user", "hooks")
MAX_UPDATE_ATTEMPTS = 5

ChangeListener = Callable[[Doc, str, str], None]


@dataclass(frozen=True)
class Edit:
    """A user change on the canvas that the agent has not seen yet."""

    id: int
    doc_id: str
    kind: str
    text: str
    box_id: str
    created_at: float


@dataclass(frozen=True)
class RevisionInfo:
    revision: int
    author: str
    reason: str
    created_at: float


class CanvasStore:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.time) -> None:
        self._conn = conn
        self._clock = clock
        self._lock = threading.RLock()
        self._listeners: list[ChangeListener] = []
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def on_change(self, listener: ChangeListener) -> None:
        """Call `listener(doc, author, reason)` after every saved revision."""
        self._listeners.append(listener)

    # -- documents -------------------------------------------------------------------------

    def draw(self, session_id: str, title: str, root: str, boxes: list[Box]) -> Doc:
        """Create the session's canvas, or replace its plan with a new revision.

        Box state (status, writes, check result) is kept for boxes that keep their id, so a redraw
        does not erase work that already happened.
        """
        validate_plan(boxes)
        with self._lock:
            for _ in range(MAX_UPDATE_ATTEMPTS):
                fresh = [Box.from_dict(b.to_dict()) for b in boxes]
                existing = self.for_session(session_id)
                if existing is None:
                    now = self._clock()
                    doc = Doc(id=_new_id(), session_id=session_id, title=title, root=root, created_at=now)
                    reason = "drew the plan"
                else:
                    doc = existing
                    old = {b.id: b for b in existing.boxes}
                    for box in fresh:
                        prev = old.get(box.id)
                        if prev is not None:
                            _carry_state(prev, box)
                    doc.title = title
                    doc.root = root
                    reason = "redrew the plan"
                doc.boxes = fresh
                saved = self._save(doc, "agent", reason)
                if saved is not None:
                    return saved
            raise CanvasError("the canvas changed too often while saving; try again")

    def get(self, doc_id: str) -> Doc | None:
        with self._lock:
            row = self._conn.execute("SELECT body FROM docs WHERE id = ?", (doc_id,)).fetchone()
        return Doc.from_dict(json.loads(row[0])) if row else None

    def for_session(self, session_id: str) -> Doc | None:
        """The most recently updated canvas of a session."""
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM docs WHERE session_id = ? ORDER BY updated_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return Doc.from_dict(json.loads(row[0])) if row else None

    def update(
        self,
        doc_id: str,
        mutate: Callable[[Doc], str | None],
        author: str,
        edits: Sequence[EditSpec] = (),
    ) -> Doc | None:
        """Load, change and save a document.

        `mutate` changes the document in place and returns a reason, or None for "no change".
        Returns the saved document, or None when nothing changed. A CanvasError from `mutate`
        leaves the stored document unchanged. When another process saved a newer revision in
        between, `mutate` runs again on the fresh document, so it must not have side effects.

        `edits` (kind, text, box_id) are queued for the agent in the same transaction as the new
        revision. `mutate` may fill the sequence; it is read after `mutate` returns.
        """
        if author not in AUTHORS:
            raise ValueError(f"unknown author {author!r}")
        with self._lock:
            for _ in range(MAX_UPDATE_ATTEMPTS):
                doc = self.get(doc_id)
                if doc is None:
                    raise CanvasError(f"no canvas with id {doc_id!r}")
                reason = mutate(doc)
                if reason is None:
                    return None
                saved = self._save(doc, author, reason, edits)
                if saved is not None:
                    return saved
            raise CanvasError("the canvas changed too often while saving; try again")

    def current_revision(self, doc_id: str) -> int | None:
        """The stored revision number. Cheap: hooks use it to find stale caches."""
        with self._lock:
            row = self._conn.execute("SELECT revision FROM docs WHERE id = ?", (doc_id,)).fetchone()
        return int(row[0]) if row else None

    def history(self, doc_id: str) -> list[RevisionInfo]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT revision, author, reason, created_at FROM revisions WHERE doc_id = ? "
                "ORDER BY revision",
                (doc_id,),
            ).fetchall()
        return [RevisionInfo(*row) for row in rows]

    def revision(self, doc_id: str, revision: int) -> Doc | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM revisions WHERE doc_id = ? AND revision = ?", (doc_id, revision)
            ).fetchone()
        return Doc.from_dict(json.loads(row[0])) if row else None

    # -- user edits waiting for the agent ---------------------------------------------------

    def add_edit(self, doc_id: str, kind: str, text: str, box_id: str = "") -> Edit:
        with self._lock:
            now = self._clock()
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO edits (doc_id, kind, text, box_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (doc_id, kind, text, box_id, now),
                )
            return Edit(int(cur.lastrowid or 0), doc_id, kind, text, box_id, now)

    def pending_edits(self, doc_id: str) -> list[Edit]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, doc_id, kind, text, box_id, created_at FROM edits "
                "WHERE doc_id = ? AND delivered_at IS NULL ORDER BY id",
                (doc_id,),
            ).fetchall()
        return [Edit(*row) for row in rows]

    def take_pending_edits(self, doc_id: str) -> list[Edit]:
        """Return the pending edits and mark them delivered, so each edit reaches the agent once."""
        with self._lock:
            edits = self.pending_edits(doc_id)
            if not edits:
                return []
            now = self._clock()
            taken: list[Edit] = []
            with self._conn:
                for edit in edits:
                    # The IS NULL guard stops two processes from both delivering one edit.
                    cur = self._conn.execute(
                        "UPDATE edits SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
                        (now, edit.id),
                    )
                    if cur.rowcount == 1:
                        taken.append(edit)
            return taken

    # -- internals -------------------------------------------------------------------------

    def _save(self, doc: Doc, author: str, reason: str, edits: Sequence[EditSpec] = ()) -> Doc | None:
        """Write the next revision in one transaction.

        Returns None, and writes nothing, when the stored revision is no longer `doc.revision`
        (another writer won). The caller reloads and tries again.
        """
        read_revision = doc.revision
        doc.revision += 1
        doc.updated_at = self._clock()
        body = json.dumps(doc.to_dict(), ensure_ascii=False)
        with self._conn:
            if read_revision == 0:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO docs (id, session_id, body, revision, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (doc.id, doc.session_id, body, doc.revision, doc.created_at, doc.updated_at),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE docs SET body = ?, revision = ?, updated_at = ? WHERE id = ? AND revision = ?",
                    (body, doc.revision, doc.updated_at, doc.id, read_revision),
                )
            if cur.rowcount != 1:
                doc.revision = read_revision
                return None
            self._conn.execute(
                "INSERT INTO revisions (doc_id, revision, author, reason, body, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (doc.id, doc.revision, author, reason, body, doc.updated_at),
            )
            self._conn.executemany(
                "INSERT INTO edits (doc_id, kind, text, box_id, created_at) VALUES (?, ?, ?, ?, ?)",
                [(doc.id, kind, text, box_id, doc.updated_at) for kind, text, box_id in edits],
            )
        for listener in list(self._listeners):
            try:
                listener(doc, author, reason)
            except Exception:
                logger.exception("hermes-canvas: change listener failed")
        return doc


def _carry_state(prev: Box, box: Box) -> None:
    box.status = prev.status
    box.owner = prev.owner
    box.writes = prev.writes
    box.written = list(prev.written)
    box.blocked = prev.blocked
    box.check_exit = prev.check_exit
    box.check_output = prev.check_output
    box.answer = prev.answer
    box.note = prev.note
    box.removed = prev.removed
    box.x, box.y = prev.x, prev.y  # only the user's `move` sets a position


def _new_id() -> str:
    return "cv-" + uuid.uuid4().hex[:10]
