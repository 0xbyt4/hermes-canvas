from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from helpers import FakeClock

from canvas.store import CanvasStore


@pytest.fixture
def store() -> Iterator[CanvasStore]:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    yield CanvasStore(conn, clock=FakeClock())
    conn.close()
