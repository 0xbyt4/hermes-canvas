from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from helpers import SHOP_PLAN, plan

from canvas.model import CanvasError, Doc, apply_op
from canvas.store import CanvasStore


def test_draw_creates_then_redraw_keeps_id_and_box_state(store: CanvasStore) -> None:
    doc = store.draw("s1", "Shop", "/repo", plan(*SHOP_PLAN))
    assert doc.revision == 1
    assert doc.id.startswith("cv-")

    def mark(d: Doc) -> str:
        box = d.require("api")
        box.status, box.writes = "done", 3
        d.require("refund").removed = True
        return "work"

    store.update(doc.id, mark, "hooks")
    redrawn = store.draw("s1", "Shop v2", "/repo", plan(*SHOP_PLAN, {"id": "email", "label": "Email"}))
    assert redrawn.id == doc.id
    assert redrawn.revision == 3
    assert redrawn.title == "Shop v2"
    api = redrawn.require("api")
    assert (api.status, api.writes) == ("done", 3)
    assert redrawn.require("refund").removed is True
    assert redrawn.require("email").status == "todo"


def test_draw_rejects_invalid_plan(store: CanvasStore) -> None:
    with pytest.raises(CanvasError):
        store.draw("s1", "x", "/r", plan({"id": "a", "label": "A", "after": ["b"]}))
    assert store.for_session("s1") is None


def test_update_none_means_no_revision(store: CanvasStore) -> None:
    doc = store.draw("s1", "Shop", "/repo", plan(*SHOP_PLAN))
    assert store.update(doc.id, lambda d: None, "hooks") is None
    assert store.get(doc.id).revision == 1


def test_update_error_leaves_doc_unchanged(store: CanvasStore) -> None:
    doc = store.draw("s1", "Shop", "/repo", plan(*SHOP_PLAN))

    def bad(d: Doc) -> str:
        d.require("api").label = "changed"
        return apply_op(d, {"op": "explode"})

    with pytest.raises(CanvasError):
        store.update(doc.id, bad, "user")
    assert store.get(doc.id).require("api").label == "Payment API"


def test_update_unknown_doc_and_author(store: CanvasStore) -> None:
    with pytest.raises(CanvasError, match="no canvas"):
        store.update("cv-missing", lambda d: "x", "agent")
    doc = store.draw("s1", "Shop", "/repo", plan(*SHOP_PLAN))
    with pytest.raises(ValueError, match="unknown author"):
        store.update(doc.id, lambda d: "x", "someone")


def test_history_and_old_revision(store: CanvasStore) -> None:
    doc = store.draw("s1", "Shop", "/repo", plan(*SHOP_PLAN))
    store.update(doc.id, lambda d: apply_op(d, {"op": "remove", "id": "refund"}), "user")
    history = store.history(doc.id)
    assert [(h.revision, h.author, h.reason) for h in history] == [
        (1, "agent", "drew the plan"),
        (2, "user", "removed refund"),
    ]
    assert store.revision(doc.id, 1).require("refund").removed is False


def test_listeners_get_every_save_and_failures_are_isolated(store: CanvasStore) -> None:
    seen: list[tuple[int, str, str]] = []

    def broken(doc: Doc, author: str, reason: str) -> None:
        raise RuntimeError("boom")

    store.on_change(broken)
    store.on_change(lambda d, a, r: seen.append((d.revision, a, r)))
    doc = store.draw("s1", "Shop", "/repo", plan(*SHOP_PLAN))
    store.update(doc.id, lambda d: "status", "hooks")
    assert seen == [(1, "agent", "drew the plan"), (2, "hooks", "status")]


def test_pending_edits_are_delivered_once(store: CanvasStore) -> None:
    doc = store.draw("s1", "Shop", "/repo", plan(*SHOP_PLAN))
    store.add_edit(doc.id, "note", "Use Stripe test keys", box_id="api")
    store.add_edit(doc.id, "remove", "Removed box refund", box_id="refund")
    taken = store.take_pending_edits(doc.id)
    assert [(e.kind, e.box_id) for e in taken] == [("note", "api"), ("remove", "refund")]
    assert store.take_pending_edits(doc.id) == []


def test_for_session_isolates_sessions(store: CanvasStore) -> None:
    a = store.draw("s1", "A", "/a", plan({"id": "x", "label": "X"}))
    b = store.draw("s2", "B", "/b", plan({"id": "y", "label": "Y"}))
    assert store.for_session("s1").id == a.id
    assert store.for_session("s2").id == b.id
    assert store.for_session("s3") is None


def two_stores(tmp_path: Path) -> tuple[CanvasStore, CanvasStore]:
    """Two stores on one database file, like the CLI and the Desktop backend."""
    path = tmp_path / "data.db"
    return (
        CanvasStore(sqlite3.connect(path, check_same_thread=False)),
        CanvasStore(sqlite3.connect(path, check_same_thread=False)),
    )


def test_concurrent_update_from_another_process_is_not_lost(tmp_path: Path) -> None:
    first, second = two_stores(tmp_path)
    doc = first.draw("s1", "Shop", "/repo", plan(*SHOP_PLAN))
    calls = 0

    def slow_mark(d: Doc) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            # Another process saves a revision after this one read the document.
            second.update(doc.id, lambda o: apply_op(o, {"op": "remove", "id": "refund"}), "user")
        d.require("api").status = "done"
        return "api done"

    saved = first.update(doc.id, slow_mark, "hooks")
    assert calls == 2
    assert saved is not None and saved.revision == 3
    stored = second.get(doc.id)
    assert stored.require("refund").removed is True
    assert stored.require("api").status == "done"
    assert [h.reason for h in first.history(doc.id)] == ["drew the plan", "removed refund", "api done"]


def test_pending_edit_is_delivered_once_across_processes(tmp_path: Path) -> None:
    first, second = two_stores(tmp_path)
    doc = first.draw("s1", "Shop", "/repo", plan(*SHOP_PLAN))
    first.add_edit(doc.id, "note", "hello")
    assert [e.text for e in second.pending_edits(doc.id)] == ["hello"]
    assert [e.text for e in first.take_pending_edits(doc.id)] == ["hello"]
    assert second.take_pending_edits(doc.id) == []


def test_current_revision(store: CanvasStore) -> None:
    doc = store.draw("s1", "Shop", "/repo", plan(*SHOP_PLAN))
    assert store.current_revision(doc.id) == 1
    assert store.current_revision("cv-missing") is None
