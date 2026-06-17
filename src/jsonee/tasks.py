"""Durable background tasks.

A thin persistence layer over the app's background executor. Each task is
one document in a store collection whose lifecycle —

    queued → running → done | error

— is updated *in place*. Tasks are registered by *kind* with a handler;
``submit`` persists the task and dispatches it; on startup ``resume``
re-dispatches any task a crash or restart left in a non-terminal state.

The contract that makes resumption safe: **payloads are JSON-serialisable
and carry no secrets.** A handler re-sources its credentials (e.g. an app
token) at run time from the payload's identifiers, so a task persisted
before a restart can be re-run afterwards without having stored anything
sensitive. A flow that genuinely needs a per-user secret to run is, by
construction, not durable through this queue.

State is kept as a single document per task (not an append-only event log):
the store is secantusdb / MongoDB over the wire, which supports in-place
``update_one`` / upsert — see :mod:`jsonee.store`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from extjson import ObjectId

LOG = logging.getLogger(__name__)

TERMINAL_STATES = ("done", "error")
LIVE_STATES = ("queued", "running")


def _utcnow():
    # tz-aware; the stack (extjson) refuses naive datetimes.
    return datetime.now(timezone.utc)


class DurableTasks:
    """Persisted, resumable background tasks over ``app.background``.

    Construct early (handlers register before ``app.start``); the store and
    executor are read lazily, so this can exist before either is wired."""

    def __init__(self, app, collection_name="_tasks"):
        self._app = app
        self._coll_name = collection_name
        self._handlers = {}
        self._indexed = False

    # --- collection / indexes ------------------------------------------
    def _coll(self):
        return self._app.store.collection(self._coll_name)

    def _ensure_indexes(self):
        if self._indexed:
            return
        try:
            self._coll().create_index([("task_id", 1)], unique=True)
            self._coll().create_index([("state", 1)])
            self._indexed = True
        except Exception:
            LOG.exception("could not create %s indexes", self._coll_name)

    # --- registration --------------------------------------------------
    def register(self, kind, fn):
        """Register handler ``fn(payload) -> result|None`` for a task kind.

        The result (a JSON-serialisable dict, or None) is stored on the
        task's terminal ``done`` event."""
        self._handlers[kind] = fn
        return fn

    # --- submit / run --------------------------------------------------
    def submit(self, kind, payload=None, task_id=None):
        """Persist a task (state ``queued``) and dispatch it.

        Returns the ``task_id``. Pass an existing ``task_id`` to re-dispatch
        a known task (what :meth:`resume` does) — the doc is updated, not
        duplicated, thanks to the upsert on ``task_id``."""
        if kind not in self._handlers:
            raise KeyError(f"no handler registered for task kind {kind!r}")
        self._ensure_indexes()
        payload = payload or {}
        task_id = task_id or str(ObjectId())
        now = _utcnow()
        self._coll().update_one(
            {"task_id": task_id},
            {"$set": {"kind": kind, "payload": payload,
                      "state": "queued", "updated_at": now},
             "$setOnInsert": {"task_id": task_id, "created_at": now}},
            upsert=True)
        self._app.background.submit(self._run, task_id, kind, payload)
        return task_id

    def _run(self, task_id, kind, payload):
        fn = self._handlers.get(kind)
        if fn is None:
            self._set(task_id, "error", error=f"no handler for kind {kind!r}")
            return
        self._coll().update_one(
            {"task_id": task_id},
            {"$set": {"state": "running", "updated_at": _utcnow()},
             "$inc": {"attempts": 1}})
        try:
            result = fn(payload) or {}
            self._set(task_id, "done", result=result, error=None)
        except Exception as e:
            LOG.exception("task %s (%s) failed", task_id, kind)
            self._set(task_id, "error", error=f"{type(e).__name__}: {e}")

    def _set(self, task_id, state, **fields):
        upd = {"state": state, "updated_at": _utcnow()}
        upd.update(fields)
        self._coll().update_one({"task_id": task_id}, {"$set": upd})

    # --- resume --------------------------------------------------------
    def resume(self):
        """Re-dispatch every task a prior run left non-terminal.

        Call once on startup, after the executor exists and all handlers are
        registered. Tasks whose kind has no registered handler are left
        untouched (a different deployment may own them). Returns the count
        re-dispatched."""
        self._ensure_indexes()
        stuck = self._coll().find({"state": {"$in": list(LIVE_STATES)}})
        n = 0
        for t in stuck:
            kind = t.get("kind")
            if kind not in self._handlers:
                LOG.warning("resume: no handler for task %s kind %r; leaving",
                            t.get("task_id"), kind)
                continue
            self.submit(kind, t.get("payload") or {}, task_id=t.get("task_id"))
            n += 1
        if n:
            LOG.info("resumed %d unfinished task(s)", n)
        return n

    # --- reads ---------------------------------------------------------
    def get(self, task_id):
        return self._coll().find_one({"task_id": task_id})

    def list(self, filter_=None):
        """Tasks matching ``filter_`` (e.g. ``{"kind": "backfill"}``),
        newest activity first."""
        return self._coll().find(filter_ or {}, sort={"updated_at": -1})
