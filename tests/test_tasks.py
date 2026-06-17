"""Tests for jsonee.tasks.DurableTasks.

Covers the contract that makes the queue useful: a submitted task runs and
records its result in place; a handler error is captured; and a task left
non-terminal by a crash is re-dispatched by resume() — without duplicating
the document. Uses a real ephemeral secantus store (the only store) and a
stub app that supplies just ``.store`` and ``.background``.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from jsonee import open_store, DurableTasks


class _StubApp:
    def __init__(self, store):
        self.store = store
        self.background = ThreadPoolExecutor(max_workers=2)


@pytest.fixture
def app():
    store = open_store()
    a = _StubApp(store)
    a.tasks = DurableTasks(a)
    try:
        yield a
    finally:
        a.background.shutdown(wait=True)
        store.stop()


def _wait(tasks, task_id, state, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        doc = tasks.get(task_id)
        if doc and doc.get("state") == state:
            return doc
        time.sleep(0.02)
    doc = tasks.get(task_id)
    raise AssertionError(
        f"task {task_id} state={doc and doc.get('state')!r} != {state!r}")


def test_submit_runs_handler_and_records_done(app):
    seen = []
    app.tasks.register("echo", lambda p: (seen.append(p["x"]), {"echoed": p["x"]})[1])
    tid = app.tasks.submit("echo", {"x": 7})
    doc = _wait(app.tasks, tid, "done")
    assert seen == [7]
    assert doc["result"]["echoed"] == 7
    assert doc["attempts"] == 1


def test_handler_exception_is_captured_as_error(app):
    def boom(_payload):
        raise ValueError("nope")
    app.tasks.register("boom", boom)
    tid = app.tasks.submit("boom", {})
    doc = _wait(app.tasks, tid, "error")
    assert "ValueError: nope" in doc["error"]


def test_submit_unregistered_kind_raises(app):
    with pytest.raises(KeyError):
        app.tasks.submit("never-registered", {})


def test_resume_redispatches_nonterminal_without_duplicating(app):
    ran = []
    app.tasks.register("work", lambda p: ran.append(p["id"]) or None)
    coll = app.store.collection("_tasks")
    # Simulate a task a crash left mid-flight: persisted 'running', handler
    # never finished. (This is exactly the state a kill during _run leaves.)
    coll.update_one(
        {"task_id": "stuck"},
        {"$set": {"task_id": "stuck", "kind": "work",
                  "payload": {"id": "A"}, "state": "running"}},
        upsert=True)
    assert app.tasks.resume() == 1
    _wait(app.tasks, "stuck", "done")
    assert ran == ["A"]
    # resume re-used the same task_id (upsert on task_id) — no duplicate doc.
    assert coll.count({"task_id": "stuck"}) == 1


def test_resume_leaves_unknown_kind_and_terminal_alone(app):
    app.tasks.register("known", lambda p: None)
    coll = app.store.collection("_tasks")
    coll.update_one({"task_id": "u"},
                    {"$set": {"task_id": "u", "kind": "unregistered-here",
                              "state": "queued"}}, upsert=True)
    coll.update_one({"task_id": "d"},
                    {"$set": {"task_id": "d", "kind": "known",
                              "state": "done"}}, upsert=True)
    assert app.tasks.resume() == 0
    assert app.tasks.get("u")["state"] == "queued"   # untouched
    assert app.tasks.get("d")["state"] == "done"
