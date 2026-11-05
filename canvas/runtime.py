"""Shared in-memory state for hooks and tools: which canvas a call belongs to, and which subagent
works on which box.

Main agent: `task_id == session_id` (verified live in CLI and Desktop). Subagent: `task_id` is the
`child_subagent_id` (`sa-<i>-<hex>`) and `session_id` is the child's own session. Subagents are
mapped back to the root session that owns the canvas through `subagent_start`.
"""

from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass

from .binding import Binding, parse_goal_tag
from .model import Doc
from .store import CanvasStore

logger = logging.getLogger(__name__)


@dataclass
class SubagentLink:
    subagent_id: str
    child_session_id: str
    root_session_id: str
    doc_id: str
    box_id: str
    finished: str = ""


class CanvasRuntime:
    def __init__(self, store: CanvasStore) -> None:
        self.store = store
        self._lock = threading.RLock()
        self._bindings: dict[str, Binding] = {}
        self._session_docs: dict[str, str] = {}
        self._subagents: dict[str, SubagentLink] = {}
        self._child_sessions: dict[str, str] = {}
        self._note_keys: dict[str, str] = {}
        store.on_change(self._on_change)

    def _on_change(self, doc: Doc, author: str, reason: str) -> None:
        self._remember(doc)
        with self._lock:
            self._session_docs[doc.session_id] = doc.id

    def _remember(self, doc: Doc) -> Binding:
        """Cache the binding for `doc` unless a newer revision is already cached."""
        built = Binding.build(doc)
        with self._lock:
            cached = self._bindings.get(doc.id)
            if cached is not None and cached.revision >= built.revision:
                return cached
            self._bindings[doc.id] = built
            return built

    def note_key(self, doc_id: str) -> str:
        """A random tag for this canvas's notes inside tool results.

        The tag is announced only in `pre_llm_call` context (the user's channel). A file, page or
        command output that imitates a canvas note cannot know it.
        """
        with self._lock:
            key = self._note_keys.get(doc_id)
            if key is None:
                key = self._note_keys[doc_id] = secrets.token_hex(4)
            return key

    # -- identity --------------------------------------------------------------------------

    def link_for(self, task_id: str, session_id: str) -> SubagentLink | None:
        with self._lock:
            link = self._subagents.get(task_id)
            if link is None and session_id:
                sub_id = self._child_sessions.get(session_id)
                link = self._subagents.get(sub_id) if sub_id else None
            return link

    def root_session(self, task_id: str, session_id: str) -> str:
        """The session that owns the canvas for a call from any agent in the tree."""
        link = self.link_for(task_id, session_id)
        if link is not None:
            return link.root_session_id
        return session_id or task_id

    def doc_id_for(self, task_id: str, session_id: str) -> str | None:
        link = self.link_for(task_id, session_id)
        if link is not None and link.doc_id:
            return link.doc_id
        root = session_id or task_id
        if link is not None:
            root = link.root_session_id
        if not root:
            return None
        with self._lock:
            cached = self._session_docs.get(root)
        if cached is not None:
            return cached
        # Not cached: the canvas may have been drawn by another process since the last look, so
        # "no canvas" is never cached.
        doc = self.store.for_session(root)
        if doc is None:
            return None
        self._remember(doc)
        with self._lock:
            self._session_docs[root] = doc.id
        return doc.id

    def binding(self, doc_id: str) -> Binding | None:
        """Matchers for the stored revision. Another process (the Desktop backend, a CLI run) can
        change the document, so the cache is checked against the stored revision number."""
        stored = self.store.current_revision(doc_id)
        if stored is None:
            return None
        with self._lock:
            cached = self._bindings.get(doc_id)
        if cached is not None and cached.revision == stored:
            return cached
        doc = self.store.get(doc_id)
        return self._remember(doc) if doc is not None else None

    def owner_box(self, task_id: str, session_id: str) -> tuple[str, str] | None:
        """(doc_id, box_id) of the box a subagent was started for, if any."""
        link = self.link_for(task_id, session_id)
        if link is None or not link.doc_id or not link.box_id:
            return None
        return link.doc_id, link.box_id

    # -- subagents -------------------------------------------------------------------------

    def register_subagent(
        self,
        subagent_id: str,
        child_session_id: str,
        parent_session_id: str,
        parent_subagent_id: str,
        goal: str,
    ) -> SubagentLink:
        root = self.root_session(parent_subagent_id or "", parent_session_id)
        doc_id, box_id = "", ""
        tag = parse_goal_tag(goal)
        if tag is not None:
            tagged_doc = self.store.get(tag[0])
            # Only a box of this session's own canvas: a goal copied from another chat must not
            # let this subagent change that chat's canvas.
            own = tagged_doc is not None and tagged_doc.session_id == root
            if own and tagged_doc is not None and tagged_doc.box(tag[1]) is not None:
                doc_id, box_id = tag
            else:
                logger.info("hermes-canvas: goal tag %s does not match a box of this session's canvas", tag)
        if not doc_id:
            parent_link = self.link_for(parent_subagent_id or "", parent_session_id)
            if parent_link is not None:
                doc_id, box_id = parent_link.doc_id, parent_link.box_id
            else:
                doc_id = self.doc_id_for("", root) or ""
        link = SubagentLink(subagent_id, child_session_id, root, doc_id, box_id)
        with self._lock:
            self._subagents[subagent_id] = link
            if child_session_id:
                self._child_sessions[child_session_id] = subagent_id
        return link

    def finish_subagent(self, child_session_id: str, status: str) -> SubagentLink | None:
        with self._lock:
            sub_id = self._child_sessions.get(child_session_id)
            link = self._subagents.get(sub_id) if sub_id else None
            if link is not None:
                link.finished = status or "unknown"
            return link
