from __future__ import annotations

import json

from canvas.hooks import CanvasHooks
from canvas.model import Box

ROOT = "/repo"
SID = "local-abc"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def plan(*specs: dict) -> list[Box]:
    return [Box.from_plan(s) for s in specs]


SHOP_PLAN = (
    {"id": "api", "label": "Payment API", "files": ["src/payments/api/**"], "check": "npm test -- api"},
    {"id": "ui", "label": "Checkout UI", "files": ["src/checkout/*.tsx"], "after": ["api"]},
    {"id": "refund", "label": "Refund page", "files": ["src/refund/**"], "after": ["api"]},
)


def write(
    hooks: CanvasHooks, path: str, *, task_id: str = SID, session_id: str = SID, status: str = "ok"
) -> None:
    hooks.post_tool_call(
        tool_name="write_file",
        args={"path": path, "content": "x"},
        result=json.dumps({"bytes_written": 1}),
        task_id=task_id,
        session_id=session_id,
        status=status,
    )


def terminal(hooks: CanvasHooks, command: str, exit_code: int, output: str = "", **extra: object) -> None:
    # Like Hermes: any non-zero exit is reported to post_tool_call as status "error".
    hooks.post_tool_call(
        tool_name="terminal",
        args={"command": command},
        result=json.dumps({"output": output, "exit_code": exit_code, "error": None, **extra}),
        task_id=SID,
        session_id=SID,
        status="ok" if exit_code == 0 else "error",
    )


def start_api_subagent(hooks: CanvasHooks, doc_id: str) -> None:
    hooks.subagent_start(
        parent_session_id=SID,
        child_session_id="c1",
        child_subagent_id="sa-0-aaaa1111",
        child_goal=f"[canvas:{doc_id}/api] Payment API.",
    )
