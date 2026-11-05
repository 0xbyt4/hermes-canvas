"""hermes-canvas: the agent plans on a canvas, the user steers, and box status comes from hooks."""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .canvas.runtime import CanvasRuntime

logger = logging.getLogger(__name__)

PLUGIN_NAME = "hermes-canvas"
SKILL_PATH = Path(__file__).parent / "skills" / "hermes-canvas" / "SKILL.md"
# Loaded by `hermes serve` (the Desktop backend). Only there is a broadcast delivered to a window.
GATEWAY_MODULE = "tui_gateway.server"

_runtime: CanvasRuntime | None = None
_runtime_lock = threading.Lock()


def get_runtime() -> CanvasRuntime | None:
    """The runtime created by register() or by the dashboard backend, if any."""
    return _runtime


def ensure_runtime() -> CanvasRuntime:
    """One runtime per process, shared by the hooks, the tools and the dashboard backend."""
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            # Imported here, not at module level: pytest imports this file outside its package.
            from plugins.plugin_storage import plugin_db

            from .canvas.api import broadcaster
            from .canvas.runtime import CanvasRuntime
            from .canvas.store import CanvasStore

            store = CanvasStore(plugin_db(PLUGIN_NAME))
            _runtime = CanvasRuntime(store)
            store.on_change(broadcaster(_emit))
        return _runtime


def _emit(event: str, payload: dict[str, Any]) -> None:
    # `hermes chat` and cron have no Desktop window: skip the import and the "dropped" log line.
    if GATEWAY_MODULE not in sys.modules:
        return
    from hermes_cli.plugin_events import broadcast_plugin_event

    broadcast_plugin_event(PLUGIN_NAME, event, payload)


def register(ctx: Any) -> None:
    from .canvas.hooks import CanvasHooks
    from .canvas.tools import TOOLSET, CanvasTools

    runtime = ensure_runtime()
    hooks = CanvasHooks(runtime)
    tools = CanvasTools(runtime)

    for schema, handler in tools.specs():
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=handler, emoji="🗺️")
    ctx.register_hook("pre_tool_call", hooks.pre_tool_call)
    ctx.register_hook("post_tool_call", hooks.post_tool_call)
    ctx.register_hook("subagent_start", hooks.subagent_start)
    ctx.register_hook("subagent_stop", hooks.subagent_stop)
    ctx.register_hook("pre_llm_call", hooks.pre_llm_call)
    ctx.register_skill(
        "hermes-canvas",
        SKILL_PATH,
        description="Plan multi-step coding work on the canvas and build it box by box.",
    )
    logger.info("hermes-canvas registered")
