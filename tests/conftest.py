from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from helpers import ROOT, SHOP_PLAN, SID, FakeClock, plan

from canvas.hooks import CanvasHooks
from canvas.runtime import CanvasRuntime
from canvas.store import CanvasStore


@pytest.fixture
def store() -> Iterator[CanvasStore]:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    yield CanvasStore(conn, clock=FakeClock())
    conn.close()


@pytest.fixture
def hooks(store: CanvasStore) -> CanvasHooks:
    return CanvasHooks(CanvasRuntime(store))


@pytest.fixture
def doc_id(hooks: CanvasHooks) -> str:
    doc = hooks.store.draw(SID, "Shop", ROOT, plan(*SHOP_PLAN))
    return doc.id
