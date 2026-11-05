"""HTTP routes for the canvas pane, mounted by Hermes under /api/plugins/hermes-canvas/.

Hermes imports this file on its own (not as part of the plugin package), so it first finds the
plugin package: the copy the agent loaded in this process when there is one, so the pane, the hooks
and the tools share one runtime. Without it, it loads the package under a private name. The store
is safe across processes either way.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

from fastapi import APIRouter, HTTPException, Query

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PRIVATE_NAME = "_hermes_canvas_backend"

router = APIRouter()
_lock = threading.Lock()
_cache: tuple[ModuleType, Any] | None = None  # (package, CanvasApi)


def _loaded_package() -> ModuleType | None:
    """The plugin package the agent loaded in this process, if any."""
    init_file = PLUGIN_DIR / "__init__.py"
    for name, module in list(sys.modules.items()):
        if name == PRIVATE_NAME:  # same file, but not the agent's copy
            continue
        path = getattr(module, "__file__", None)
        # Cheap filters first: resolve() is a system call, and sys.modules holds thousands of entries.
        if not isinstance(path, str) or not path.endswith("__init__.py") or "canvas" not in path:
            continue
        try:
            if Path(path).resolve() != init_file:
                continue
        except OSError:
            continue
        if hasattr(module, "ensure_runtime"):
            return module
    return None


def _private_package() -> ModuleType:
    if PRIVATE_NAME in sys.modules:
        return sys.modules[PRIVATE_NAME]
    spec = importlib.util.spec_from_file_location(
        PRIVATE_NAME, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the hermes-canvas package from {PLUGIN_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PRIVATE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(PRIVATE_NAME, None)
        raise
    return module


def _canvas_api() -> Any:
    """The CanvasApi on the agent's own runtime when the agent plugin is loaded here.

    The answer is cached, but checked on every call: the agent plugin can register after the first
    request (then the private copy is replaced) or be reloaded (then the new copy is used).
    """
    global _cache
    with _lock:
        if _cache is not None:
            package, api = _cache
            if package.__name__ != PRIVATE_NAME and sys.modules.get(package.__name__) is package:
                return api
        package = _loaded_package() or _private_package()
        if _cache is not None and _cache[0] is package:
            return _cache[1]
        api_module = importlib.import_module(f"{package.__name__}.canvas.api")
        api = api_module.CanvasApi(package.ensure_runtime())
        _cache = (package, api)
        return api


def _call(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:  # CanvasError: a bad edit or an unknown canvas, safe to show
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/canvas")
def get_canvas(
    session_id: str = Query("", max_length=200),
    id: str = Query("", max_length=64),
    since: int | None = Query(None, ge=0),
) -> dict[str, Any]:
    api = _canvas_api()
    return _call(api.canvas, session_id=session_id, canvas_id=id, since=since)


@router.post("/edit")
def post_edit(body: dict[str, Any]) -> dict[str, Any]:  # a dict parameter is the JSON body
    canvas_id = body.get("canvas_id")
    if not isinstance(canvas_id, str) or not canvas_id:
        raise HTTPException(status_code=400, detail="canvas_id is required")
    api = _canvas_api()
    return _call(api.edit, canvas_id, body.get("ops"))


@router.get("/edits")
def get_edits(id: str = Query(..., max_length=64), limit: int = Query(8, ge=1, le=20)) -> dict[str, Any]:
    return _call(_canvas_api().edits, id, limit)


@router.get("/history")
def get_history(id: str = Query(..., max_length=64)) -> dict[str, Any]:
    return _call(_canvas_api().history, id)


@router.get("/diff")
def get_diff(id: str = Query(..., max_length=64), box: str = Query(..., max_length=64)) -> dict[str, Any]:
    return _call(_canvas_api().diff, id, box)


@router.get("/revision")
def get_revision(id: str = Query(..., max_length=64), n: int = Query(..., ge=1)) -> dict[str, Any]:
    return _call(_canvas_api().revision, id, n)
