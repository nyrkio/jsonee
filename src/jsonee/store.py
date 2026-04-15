"""Storage adapter. v0 ships an in-memory implementation with a
FerretDB/MongoDB-shaped interface so swapping to the real DB later is
mechanical. Query logic lives in purejson — this module is just a container
that names a collection and hands out query access.

Optional persistence: if constructed with ``snapshot_path``, the store
restores from that file on startup (if it exists) and dumps to it
periodically on a background thread. The file is Extended JSON so
ObjectIds and datetimes round-trip losslessly."""
import os
import threading

from purejson import Document, Collection
from extjson import ObjectId, dumps as _dumps, loads as _loads


class Store:
    """Abstract interface. Duck-typed, not statically typed."""

    def collection(self, name):
        raise NotImplementedError


class _Collection:
    def __init__(self):
        self._docs = Collection()

    def insert_one(self, doc):
        raw = dict(doc.data) if isinstance(doc, Document) else dict(doc)
        if "_id" not in raw:
            raw["_id"] = ObjectId()
        self._docs.append(raw)
        return raw["_id"]

    def insert_many(self, docs):
        return [self.insert_one(d) for d in docs]

    def find_one(self, filter_=None):
        return self._docs.find_one(filter_ or {})

    def find(self, filter_=None, sort=None, limit=None):
        return self._docs.query(filter_ or {}, sort=sort, limit=limit)

    def count(self, filter_=None):
        if not filter_:
            return len(self._docs.data)
        return len(self._docs.query(filter_).data)


class InMemoryStore(Store):
    def __init__(self, snapshot_path=None, snapshot_interval_s=60.0):
        """In-memory store, optionally persisted to disk.

        - ``snapshot_path``: if given, the store loads from this file on
          startup (when it exists) and dumps to it on a background
          thread every ``snapshot_interval_s`` seconds. Use ``None``
          for a pure in-memory store (tests, short-lived processes).
        - ``snapshot_interval_s``: 0 or negative disables the periodic
          thread — you can still snapshot manually via ``.snapshot()``.
        """
        self._collections = {}
        self._snapshot_path = snapshot_path
        self._snapshot_interval_s = snapshot_interval_s
        self._snapshot_thread = None
        self._stop_event = None
        if snapshot_path:
            self.restore()
            if snapshot_interval_s and snapshot_interval_s > 0:
                self._start_snapshot_thread()

    def collection(self, name):
        if name not in self._collections:
            self._collections[name] = _Collection()
        return self._collections[name]

    # ---- persistence -----------------------------------------------------

    def snapshot(self, path=None):
        """Atomically dump the whole store to Extended JSON at `path`.

        Defaults to the ``snapshot_path`` supplied to the constructor.
        Writes to ``<path>.tmp`` and renames — a crashed writer can
        never leave a half-written snapshot in place of a good one.
        """
        path = path or self._snapshot_path
        if not path:
            raise ValueError("no snapshot_path configured")
        # Snapshot the collection contents as plain lists. list() on the
        # underlying storage is a single-opcode copy under the CPython
        # GIL so concurrent inserts during the copy are safe.
        data = {name: list(coll._docs.data)
                for name, coll in self._collections.items()}
        payload = _dumps(data)
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)

    def restore(self, path=None):
        """Load collections from `path` if it exists; no-op otherwise."""
        path = path or self._snapshot_path
        if not path or not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            data = _loads(f.read())
        for name, docs in data.items():
            coll = self.collection(name)
            for d in docs:
                coll._docs.append(d)

    def _start_snapshot_thread(self):
        self._stop_event = threading.Event()

        def loop():
            # Use Event.wait as the interval sleep so `stop()` wakes us
            # immediately for a final flush.
            while not self._stop_event.wait(self._snapshot_interval_s):
                try:
                    self.snapshot()
                except Exception:
                    import traceback
                    traceback.print_exc()

        t = threading.Thread(target=loop, daemon=True,
                             name="InMemoryStore-snapshot")
        t.start()
        self._snapshot_thread = t

    def stop(self):
        """Stop the background snapshot thread and do a final dump."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._snapshot_path:
            try:
                self.snapshot()
            except Exception:
                import traceback
                traceback.print_exc()
