"""hermes-canvas: the agent plans on a canvas, the user steers, and box status comes from hooks."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .canvas.runtime import CanvasRuntime

logger = logging.getLogger(__name__)

PLUGIN_NAME = "hermes-canvas"
SKILL_PATH = Path(__file__).parent / "skills" / "hermes-canvas" / "SKILL.md"

_runtime: CanvasRuntime | None = None


def get_runtime() -> CanvasRuntime | None:
    """The runtime created by register(); the dashboard backend shares it in-process."""
    return _runtime


def register(ctx: Any) -> None:
    global _runtime
    # Imported here, not at module level: pytest imports this file outside its package.
    from plugins.plugin_storage import plugin_db

    from .canvas.hooks import CanvasHooks
    from .canvas.runtime import CanvasRuntime
    from .canvas.store import CanvasStore
    from .canvas.tools import TOOLSET, CanvasTools

    _runtime = CanvasRuntime(CanvasStore(plugin_db(PLUGIN_NAME)))
    hooks = CanvasHooks(_runtime)
    tools = CanvasTools(_runtime)

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
